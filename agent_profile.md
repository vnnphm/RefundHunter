![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

# Refund Hunter

Finds money you're owed on things you already bought.

1. **Add your purchases:** connect Gmail (read-only, recent order emails, nothing stored), upload a receipt photo or screenshot, or paste a product link.
2. **Review:** every receipt shows up as a card. Approve the items you want tracked. Items without a link get matched to the store's listing, and you pick the right one.
3. **Track:** Refund Hunter checks each item's live price on Amazon and Walmart, re-checks it every few hours, and messages you when it drops.
4. **Get it back:** on a drop it says what you can ask for and by when (price adjustment, return & rebuy, or a goodwill credit), and drafts the message to paste into the store's chat. You send it; we never contact a store or log in for you.

## Try it

- `@refundhunter hi`
- `@refundhunter connect my gmail`
- `@refundhunter any new receipts?`
- `@refundhunter track https://www.amazon.com/dp/B09B221Q7K I paid $25`
- `@refundhunter show my items`
- `@refundhunter check my prices`
- Upload a photo of a receipt in the chat

## How it works

Five uAgents, one job each: this coordinator (chat, cards), a price agent (Bright Data live prices and store search), a parser agent (asi1 reads receipt photos and order emails), a policy agent (which refund to ask for, and the deadline), and a claim agent (asi1 writes the refund request). Built for Cal Hacks 13.0, Fetch.ai track.
