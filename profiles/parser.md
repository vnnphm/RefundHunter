![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

# Refund Hunter: receipt reader

Turns a receipt into data Refund Hunter can track: the store, purchase date, order number, and each item with its price.

- **Receipt photos and screenshots** uploaded in ASI:One, read by ASI:One's own model (asi1). No other AI provider.
- **Order emails** imported with Connect Gmail: the same prompt reads the email text, so photos and emails give the same result.
- Leaves out tax, shipping, discounts and totals, and never guesses a price it can't read. Only the parsed fields are kept, never the image or the email.

## How to use it

It isn't a chat agent: talk to **@refundhunter** in ASI:One, which sends your receipt here.

- Upload a photo or screenshot of a receipt in the chat
- `@refundhunter connect my gmail`
- `@refundhunter any new receipts?`

## Messages

- `Receipt` (a photo link or email text) → `ParsedReceipt` (store, purchase date, order number, items with prices and links, or an error)

Part of Refund Hunter, built for Cal Hacks 13.0 (Fetch.ai track).
