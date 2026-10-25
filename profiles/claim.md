![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

# Refund Hunter: claim writer

Drafts the refund request a shopper pastes into a store's customer service chat after a price drop.

- Written by ASI:One's model (asi1): short, polite, first person, and asking for exactly one thing (a price adjustment, a goodwill credit, or help to return and rebuy).
- Uses only the facts it's given: the item, prices, order number, purchase date and deadline. It never invents a policy, date or order number.
- Falls back to a fixed template if asi1 is slow or unavailable, so there's always a message to send.
- The shopper sends it; Refund Hunter never contacts a store or logs in for anyone.

## How to use it

It isn't a chat agent: talk to **@refundhunter** in ASI:One. On a price drop, tap **Draft a refund request**.

- `@refundhunter check my prices`
- `@refundhunter draft a refund request`

## Messages

- `ClaimRequest` (store, item, prices, purchase date, order number, what to ask for, deadline) → `ClaimDraft` (the message, where to send it, or an error)

Part of Refund Hunter, built for Cal Hacks 13.0 (Fetch.ai track).
