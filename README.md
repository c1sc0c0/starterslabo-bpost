# bpost — parcel orders from a Shopify order or a pasted address

Unofficial. Not affiliated with bpost.

Builds a bpost **Verzendplatform** cart (guest or signed in), stops before payment, and gives you
either the bpost basket link or a MultiSafepay checkout link. Written for a small Belgian shop's daily shipping.

The skill **never pays** and never sees card data: the money step happens on bpost's own payment screen
or on the hosted checkout page the payment link opens.

## Why it is built this way

`parcel.bpost.be` is an Angular SPA. The cart lives server-side under an `orderId`, but the SPA only
reads that id from `sessionStorage`. A guest cart therefore has **no shareable URL** — `/nl/basket`,
`/nl/basket/<id>`, `/nl/order/<id>`, `/nl/checkout/<id>`, `/nl/pay?orderId=` all fail (verified).

Signed in, the cart is the account's `order-in-progress`, so `https://parcel.bpost.be/nl/basket` just
works — laptop, phone, anywhere. That is the recommended mode. Details and the full API map:
[reference.md](reference.md).

## Install

Clone this repo **as the skill folder** (`SKILL.md` at the root):

```bash
# Cursor — this project
git clone https://github.com/c1sc0c0/starterslabo-bpost.git .cursor/skills/starterslabo-bpost

# Cursor — all projects
git clone https://github.com/c1sc0c0/starterslabo-bpost.git ~/.cursor/skills/starterslabo-bpost

# Claude Code — this project
git clone https://github.com/c1sc0c0/starterslabo-bpost.git .claude/skills/starterslabo-bpost
```

Start a new agent chat so the skill is picked up. The skill id in `SKILL.md` is **`bpost-label`** (the
repo is `starterslabo-bpost`, following the other skill repos). No packaging step, no third-party Python
packages — stdlib only, Python 3.10+.

```bash
cd .cursor/skills/starterslabo-bpost     # or ~/.cursor/skills/starterslabo-bpost
cp sender.example.yaml sender.yaml
$EDITOR sender.yaml                     # the return address printed on every label
```

Credentials for the signed-in mode live in the **macOS Keychain**:

```bash
security add-generic-password -s scout_bpost -a email -w 'you@example.com' -U
security add-generic-password -s scout_bpost -a password -w 'your-bpost-password' -U

python3 .cursor/skills/starterslabo-bpost/scripts/bpost_order.py login     # signs in, stores cookies
python3 .cursor/skills/starterslabo-bpost/scripts/bpost_order.py whoami    # check it is still valid
```

`login` writes `.bpost-session` (mode 600, gitignored). On first login it also finishes bpost's
account-type registration as **private**, which removes the "Voor mezelf / Voor mijn bedrijf" modal
that otherwise covers the cart.

Skip all of that and everything still works as a guest — you just lose the plain basket URL.

## Quickstart

```bash
B=.cursor/skills/starterslabo-bpost/scripts/bpost_order.py

# price only (no order created)
python3 $B --shopify 1002 --quote

# create the order, stop before payment
python3 $B --shopify 1002 --split line

# same, from an address pasted straight out of a chat or a webshop
python3 $B --to "Jane Doe, Beispielweg 5, 67655 Kaiserslautern, Germany" --weight 9.52

# hand over a checkout link only when a URL is actually needed
python3 $B pay  orderId-…
# review sheet (printable HTML mirror of the bpost basket)
python3 $B open orderId-…
```

## Address paste

Any of these work — commas, newlines, or both:

```
Jane Doe
Beispielweg 5
67655 Kaiserslautern
Germany
```

```
Jan Peeters, Voorbeeldstraat 51 bus 3, 2000 Antwerpen, België
```

The parser finds the postcode line, the street + house number (including `bus`/`box`/`bte`), the country
(full names in nl/en/fr/de, resolved against bpost's own country catalog), and treats what is left as
the name. Belgian addresses are then canonicalised through bpost's address proofing API.

Weight is required with `--to` (`--weight 9.52`, kg). Delivery type defaults to pickup point for
Belgian addresses; use `--delivery address` or `--delivery locker`.

Add `--insurance` for bpost's WARRANTY cover (€1.55 per parcel, up to €500 of goods). bpost only offers
it domestically — international shipments are refused rather than silently sent uninsured.

## Commands

| Command | What it does |
|---|---|
| `--shopify 1002` | build from a Shopify order (`--split line` = parcel per carton, `--split single` = one parcel) |
| `--to "<address>"` | build from a pasted address |
| `--json file.json` | build from a batch file |
| `--insurance` | add bpost WARRANTY cover (+€1.55/parcel, BE only, up to €500) |
| `--quote` | price only, nothing created |
| `--dry-run` | real draft, real price, then deleted (guest order, never touches your cart) |
| `pay <id>` | mint the MultiSafepay checkout link |
| `open <id>` | write `review-<id>.html`, a printable mirror of the basket |
| `review <id>` | print the order as bpost has it |
| `login` / `whoami` | keychain sign-in, session check |

Records land in `records/<orderId>.json` next to the skill (gitignored); override with
`BPOST_OUTPUT_DIR` or `output_dir:` in `config.yaml`.

## Notes

- Shopify access: set `SHOPIFY_SHOP` and `SHOPIFY_ADMIN_TOKEN`, or store an admin token in Keychain
  under `SHOPIFY_KEYCHAIN_SERVICE` (default `scout_shopify`, accounts `api` or `client_id` +
  `client_secret`).
- `--guest` forces an anonymous cart, `--cookie` overrides the session for one run,
  `BPOST_SESSION` / `--keychain-service` point somewhere else.
- Emptying the cart in the browser leaves an empty in-progress order behind; `create` clears it first,
  because bpost keeps pointing at it otherwise.
- See [SKILL.md](SKILL.md) for the agent workflow and guardrails, [reference.md](reference.md) for the
  reverse-engineered API.
