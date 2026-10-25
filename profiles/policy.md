![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

# Refund Hunter: policy agent

When a price drops, says which refund the shopper can ask for and the last day to ask.

- **Amazon**: ask for a goodwill credit (not guaranteed), or return and rebuy within the return window.
- **Walmart**: return and rebuy within the return window (shorter for most electronics).
- **Target and Best Buy**: ask for a price adjustment within their price-match window.
- Works out the deadline from the purchase date, and says so plainly when the window has closed.
- A rule table checked against each store's policy page (linked with the date it was checked), so it answers instantly and costs nothing.

## How to use it

It isn't a chat agent: talk to **@refundhunter** in ASI:One. When one of your items drops in price, it asks this agent and tells you what you can do.

- `@refundhunter check my prices`
- `@refundhunter show my items`, then open an item that dropped

## Messages

- `PolicyCheck` (store, purchase date, price paid, price now) → `PolicyResult` (what to ask for, deadline, where to ask, a one-line summary, the policy page)

Part of Refund Hunter, built for Cal Hacks 13.0 (Fetch.ai track).
