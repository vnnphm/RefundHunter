![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

# Refund Hunter: price agent

Gets the live price of a product you bought, and finds a store's listing when a receipt only has the item's name.

- **Live prices** on Amazon, Walmart, Target and Best Buy through Bright Data's ready-made Scraper API. Prices are cached for 10 minutes so repeat checks are free.
- **Store search**: a receipt item without a link (a paper receipt, a screenshot, or a Walmart email) is searched on Amazon or Walmart, and the top 3 matches go back so the shopper picks the right one. Sponsored listings are shown last.
- Jobs run in the background, so a slow scrape never holds up the next request.
- Works only for Refund Hunter's coordinator: requests from other agents get "not allowed".

## How to use it

It isn't a chat agent: talk to **@refundhunter** in ASI:One, which calls this agent for you.

- `@refundhunter track https://www.amazon.com/dp/B09B221Q7K I paid $25`
- `@refundhunter check my prices`
- Upload a receipt photo, approve it, and pick your item from the matches

## Messages

- `TrackItem` (item id, product link, store) → `PriceResult` (current price, title, time checked, or an error)
- `FindProduct` (item name, store) → `FindResult` (up to 3 matches: name, link, price, image)

Part of Refund Hunter, built for Cal Hacks 13.0 (Fetch.ai track).
