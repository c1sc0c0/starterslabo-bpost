---
name: bpost-label
description: >-
  Build bpost parcel orders (guest or signed-in on parcel.bpost.be) from a
  Shopify order or an address pasted in chat, stop for review, then
  hand back the bpost basket link or a MultiSafepay checkout link. Use when the
  user asks for bpost labels/orders, to ship a Shopify order with bpost, to
  quote bpost parcel prices, or to turn an address into a payable bpost cart
  without paying inside the agent.
---

# bpost — orders from a Shopify order or a pasted address

Unofficial. Not affiliated with bpost. Chrome/agent side: no bpost login is required for the guest
flow; the signed-in flow uses Keychain credentials.

**The skill never pays.** It builds the cart, stops, and hands the human a bpost basket link (signed in)
or a hosted checkout link (on request).

## Workflow

```
Progress:
- [ ] 1. Get the shipment source: Shopify order number, or the address pasted in chat
- [ ] 2. Quote (no writes)
- [ ] 3. Dry run (real draft, real price, deleted again; guest order, cart untouched)
- [ ] 4. Create -> prints REVIEW, nothing payable
- [ ] 5. Hand over https://parcel.bpost.be/nl/basket (signed in) or a seeded tab (guest)
- [ ] 6. `pay <id>` only if a portable link is explicitly wanted
```

**Step 5 is the default handoff, not step 6.** With a session the order sits on the account's
`order-in-progress`, so the plain basket URL works on any device. Only mint a MultiSafepay link when the
human asks for a URL (phone, another device, later).

## Addresses pasted in chat

Pass the text through `--to`, verbatim, commas or newlines, and add the weight:

```bash
python3 scripts/bpost_order.py \
  --to "Jane Doe, Beispielweg 5, 67655 Kaiserslautern, Germany" --weight 9.52
```

The parser takes the postcode line as zip+city, the line with a house number as street (+ `bus`/`box`),
matches the country name against bpost's catalog (nl/en/fr/de), and treats the remainder as the name.
Belgian addresses get canonicalised via bpost's proofing API (`--no-normalize` to skip). If the paste is
ambiguous, echo the parsed plan back to the human before creating anything.

Ask for anything missing: weight (required), delivery type (defaults to pickup point in BE), and the
email for tracking.

## Shopify orders

```bash
python3 scripts/bpost_order.py --shopify 1002 --split line
```

`--split line` = one parcel per line item (matches one label per carton), `--split single` = whole order
in one parcel. Quote both when the order has several cartons; the gap is real (2 cartons to DE: €33.00
vs €42.20). Weight comes from the line items, so never ask for it.

## Commands

| Command | Effect |
|---|---|
| `create` (default) | build the order, print REVIEW, **nothing payable** |
| `create --quote` | price only, nothing created |
| `create --dry-run` | draft order, real price, then deleted (forced to a guest order) |
| `pay <orderId>` | mint the hosted checkout link |
| `open <orderId>` | write a printable HTML mirror of the basket |
| `review <orderId>` | read-only re-print |
| `login` / `whoami` | Keychain sign-in / session check |

## Setup

Once per machine:

```bash
cp sender.example.yaml sender.yaml   # return address
security add-generic-password -s scout_bpost -a email -w 'you@example.com' -U
security add-generic-password -s scout_bpost -a password -w '…' -U
python3 scripts/bpost_order.py login
```

`login` posts the bpost Ping form, follows the OAuth callback, stores `parcel.bpost.be` cookies in
`.bpost-session` (mode 600, gitignored) and completes the account-type registration as **private** when
bpost reports it incomplete — without that, a "Voor mezelf / Voor mijn bedrijf" modal covers the cart.
If bpost asks for a one-time code, `login` prompts on stdin: run it in a terminal, not from the agent.

Never print the password or the cookie. No `-w` echo, no `cat .bpost-session`, no logs.

## Delivery and weight

| `--delivery` | Recipient gets | Notes |
|---|---|---|
| `pickup` (default) | pickup point near their postcode | domestic only |
| `address` | home delivery | required for non-BE |
| `locker` | bpost Bbox | domestic only |

`--pickup "EIERMARKT"` or a point id narrows the choice; otherwise the nearest point is used and printed
in the plan so the human can veto it before anything is created.

`--insurance` adds bpost WARRANTY (€1.55/parcel, cover up to €500). Domestic only: bpost does not offer
it for international destinations, and the CLI refuses rather than shipping uninsured.

Weight bands: 0-2 / 2-5 / 5-10 / 10-20 / 20-30 kg (`DOM_*` domestic, `INT_*` international). Over 30 kg
is rejected — split the parcel or use the `order-ship-mode` skill. The product line is the cheapest one
that supports country + destination + band (`--product-line` overrides); international ECONOMY is often
limited to 0-2 kg.

## Guardrails

- Quote, then dry run, then create. A created order is a real (unpaid) bpost order that expires.
- Do not mint a payment link unless the human asked (`pay` or `--pay-link`).
- Never open, pay, shorten or re-host the payment link. Never drive the payment page.
- If the cart is emptied in the browser, bpost keeps an empty `order-in-progress` that shadows new
  orders; `create` deletes it first — do not "fix" it by creating more orders.
- Recipient email is the customer's; bpost sends them tracking from it.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| cart empty in the browser | signed into another bpost account, or an older session | `whoami`, compare email; re-run `login` |
| "Voor mezelf / Voor mijn bedrijf" modal | account registration incomplete | re-run `login` (it completes it as private) |
| new order not in the cart | stale empty in-progress order | `create` clears it; otherwise delete the empty order |
| `bpost login failed: We didn't recognize…` | wrong Keychain password | update the Keychain item |
| session rejected, commands continue as guest | expired session | `python3 … login` |

Files: [README.md](README.md) (usage) · [reference.md](reference.md) (API map) ·
`sender.yaml` (return address) · `records/` (order JSON + review sheets).
