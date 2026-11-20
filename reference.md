# bpost parcel platform — API map & notes

Everything here was verified against the live guest flow on 2026-11-20. The site is an Angular SPA on
`https://parcel.bpost.be`; all API calls are same-origin under `/customer/…`, JSON, and require no
authentication for the guest path. No cookies/XSRF token were needed on the endpoints below.

## Flow

```
parcel-detail (destination + weight + options)
  POST /customer/parcel/product      price/validate the parcel shell
recipient-detail
  POST /customer/parcel/recipient    recipient + chosen pickup point / address
sender-detail
  POST /customer/parcel/sender       sender block -> parcel becomes part of the order
basket
  GET  /customer/orders/{orderId}?lang=nl
pay
  POST /customer/payment/orders      -> MultiSafepay paymentUrl
```

`orderId-<uuid>` and `parcelId-<uuid>` are generated client-side; the server creates the order on the
first POST. `GET /customer/orders/{orderId}` is anonymous, so the order data is not secret.

The basket page, however, is gated by `sessionStorage.orderId` in the SPA, not by the server:

| Attempt | Result |
|---|---|
| `/nl/basket?orderId=…` | ignored, empty basket, and the app mints a fresh orderId |
| `/nl/recipient-detail/parcelId-…` | empty form, no draft restored |
| `sessionStorage.setItem("orderId", …)` then `/nl/basket` | **works** — full basket |

That last line is how the human reviews a created order in the agent browser. Nothing is payable
until `POST /customer/payment/orders` is called (`pay`), which returns the MultiSafepay link.

## Endpoints used by the skill

| Method | Path | Purpose |
|---|---|---|
| POST | `/customer/catalog/search` | price catalog: product lines, destinations, weight ranges, states |
| GET | `/customer/location/delivery-points` | pickup points (`country`, `destination`, `postalCode`, `streetName`) |
| POST | `/customer/parcel/product` | create/price one parcel shell |
| POST | `/customer/parcel/recipient` | recipient block (pickup point, address, or locker) |
| POST | `/customer/parcel/sender` | sender block; after this the parcel counts in the order |
| GET | `/customer/orders/{orderId}?lang=nl` | basket state + `orderSummary.total` |
| DELETE | `/customer/orders/{orderId}?lang=nl` | empty the basket (used by `--dry-run`) |
| POST | `/customer/payment/orders` | `{gateway, language, orderId, description}` → `{paymentUrl}` |
| POST | `/customer/user/registration/residential` | `{language}` → finishes a private account registration |
| GET | `/customer/user/order-in-progress` | the account's current cart (404 when there is none) |
| GET | `/customer/payment/orders/{orderId}/status?lang=&transactionId=&feedBackIndication=` | payment status (future) |

Address proofing (different host, no auth):

```
GET https://api.mailops.bpost.cloud/roa-info/externalMailingAddressProofingRest/autocomplete/locality?q=2000
GET …/street?q=Voorbeeldstraat&postalCode=2000&locality=ANTWERPEN
GET …/streetNumber?q=51&postalCode=2000&street=VOORBEELDSTRAAT
```

## Payload shapes

Parcel shell (domestic; `surcharge` is the €1 year-end surcharge block and is `null` abroad):

```json
{
  "parcelId": "parcelId-…", "orderId": "orderId-…",
  "surcharge": {"status": "WARNING", "surchargeWarningStartDate": "2026-10-26",
                "surchargeActualStartDate": "2026-11-27", "surchargeActualEndDate": "2026-12-24",
                "priceDetails": {"priceExcludingVat": 1, "vat": 0, "priceIncludingVat": 1},
                "vat": 0, "price": 1},
  "parcelDetails": {"countryCode": "BE", "destinationCode": "PICKUP_POINT",
                    "weightRangeCode": "DOM_0_2", "senderReference": null,
                    "productTypeCode": null, "productLineCode": "STANDARD",
                    "selectedOptions": null}
}
```

International adds a sibling block and usually switches the product line:

```json
"international": {"nonDeliveryInstructions": "RETURN_TO_SENDER_NON_PRIORITY",
                  "contentDescription": null, "shipmentType": null, "parcelValue": null}
```

Recipient — three shapes:

```json
// pickup point
{"contactType": "INDIVIDUAL", "firstName": "…", "lastName": "…", "company": null,
 "notificationLang": "nl", "phoneNumber": null, "postalCode": "2000",
 "addressStreet": "EIERMARKT", "addressStreetNo": "33-35", "notificationType": "EMAIL",
 "emailAddress": "…", "phoneNumberToDisplay": "",
 "pickupPoint": {"address": {"countryCode": "BE", "postalCode": "2000", "city": "ANTWERPEN",
                             "street": "EIERMARKT", "streetNumber": "33-35"},
                 "locationId": "202100", "locationName": "ANTWERPEN EIERMARKT",
                 "notificationLanguage": "nl", "notificationType": "EMAIL"}}

// home address  (note the nested address object — the flat fields alone give HTTP 400)
{"…": "…", "address": {"countryCode": "DE", "postalCode": "67655", "city": "KAISERSLAUTERN",
                       "street": "Beispielweg", "streetNumber": "56", "boxNumber": ""}}

// locker (Bbox) — same point block as a pickup point, keyed "locker", plus lockerType
{"…": "…", "locker": {"address": {…}, "locationId": "42063",
                      "locationName": "BBOX GRAND BAZAR EIERMARKT", "lockerType": "CLASSIC",
                      "notificationLanguage": "nl", "notificationType": "EMAIL"}}
```

Locker points come from the same `delivery-points` endpoint with `destination=LOCKER` (25 points around
2000 Antwerpen, `servicePoint.lockerType` says CLASSIC or the newer cabinet type).

Sender (individual; `contactType: COMPANY` works too, but a VAT number must pass the Belgian checksum
or bpost answers a bare HTTP 400):

```json
{"contactType": "INDIVIDUAL", "title": "", "firstName": "…", "lastName": "…", "emailAddress": "…",
 "company": null, "vatNumber": null, "phoneNumber": null, "addressCityCode": "2000",
 "addressStreet": "VOORBEELDSTRAAT", "addressStreetNo": "51", "addressBoxNo": "",
 "address": {"countryCode": "BE", "postalCode": "2000", "city": "ANTWERPEN",
             "street": "VOORBEELDSTRAAT", "streetNumber": "51", "boxNumber": ""},
 "phoneNumberToDisplay": ""}
```

## Codes

- **Destination:** `PICKUP_POINT`, `ADDRESS`, `LOCKER`
- **Weight ranges:** `DOM_0_2`, `DOM_2_5`, `DOM_5_10`, `DOM_10_20`, `DOM_20_30`; international `INT_*`
- **Product lines:** `STANDARD`, `ECONOMY`, `LOCKER_TO_LOCKER` (availability differs per country/weight;
  `catalog/search` returns `state` = SELECTED / ENABLED / DISABLED)
- **Payment gateway:** the site posts the chosen method uppercased. `MOLLIE` was accepted and returned a
  MultiSafepay `connect` URL; the hosted page lets the user pick the actual method.

## Example totals (2026-11-20)

| Shipment | Price |
|---|---|
| BE pickup point 0-2 kg | €5.40 |
| BE pickup point 2-5 kg | €5.60 |
| BE home address 0-2 kg | €7.10 |
| BE home 10-20 kg | €10.15 |
| DE home 2-5 kg (STANDARD) | €16.50 |
| DE home 5-10 kg (STANDARD) | €16.50 |
| DE home 0-2 kg (ECONOMY) | €11.60 |

The €1 domestic surcharge (27/11-24/12/2026) is already included in the parcel payload.

## Not built yet (deliberately)

After payment the labels can be pulled without an account:

```
GET /customer/document/orders/{orderId}/labels/{parcelId}      (blob, needs the paid order)
POST /customer/orders/paid-guest-order/search                   (guest label lookup)
```

bpost emails the label PDFs anyway, so the skill stops at the payment link. Add the download step only
after watching one real paid order, and remember the app attaches a `g-recaptcha-token` header on some
retrieve-labels calls.

## Account behaviour that bites

- **`order-in-progress` points at the most recently touched order, not "the one with items".**
  Emptying the cart in the browser leaves an empty order behind and bpost keeps returning it, so a newly
  created order is shadowed. `create` therefore deletes an empty in-progress order before building.
  (Observed 2026-11-20: after deleting a 2-parcel order, `numberOfItems` stayed 0 and a fresh order was
  not shown until the empty one was removed.)
- **Orders are bound to `psbUserId`.** A guest order has `psbUserId: null` and can never be adopted by an
  account; changing mode means rebuilding the order.
- **`registrationComplete: false`** makes the SPA open the "Voor mezelf / Voor mijn bedrijf" modal over
  the cart on every visit. `POST /customer/user/registration/residential` with `{"language":"nl"}` fixes
  it (the account stays `customerType: private`).
- **A dry run must not use the account session** — creating and deleting an order moves the
  `order-in-progress` pointer to the deleted one. The CLI forces `--dry-run` to a guest order.

## Address parsing (client side)

`--to` accepts a pasted block in either shape and is parsed locally, no API needed:

```
Jane Doe                Jan Peeters, Voorbeeldstraat 51 bus 3, 2000 Antwerpen, België
Beispielweg 5
67655 Kaiserslautern
Germany
```

- postcode line: `^(?:B-)?(\d{4,5})\s+(.+)$` → zip + city
- street line: street + `\d+` house number, optional `bus|box|bte|bis|apt|app|unit <n>`
- country: last token, matched against `/customer/catalog/countries?lang={nl,en,fr,de}` plus a small
  fallback map of local names (`Duitsland`, `België`, `Pays-Bas`, …)
- remainder: name (first token = first name)

Belgian results are then canonicalised through the proofing API (same endpoints listed above), so
`Voorbeeldstraat` becomes `VOORBEELDSTRAAT` and `Antwerpen` gets its official spelling.
