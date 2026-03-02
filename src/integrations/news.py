"""
News via NewsAPI.org — free tier: 100 requests/day.

Supports topic routing:
  "top"       → top US headlines
  "ai"        → artificial intelligence / machine learning news
  "business"  → business & markets headlines
  "investing" → stocks, investing, markets news
  any string  → treated as a search query
"""

import logging
from datetime import datetime, timezone, timedelta
import httpx

logger = logging.getLogger(__name__)

EVERYTHING_URL = "https://newsapi.org/v2/everything"
TOP_HEADLINES_URL = "https://newsapi.org/v2/top-headlines"

# Maps topic aliases to NewsAPI params
TOPIC_CONFIGS: dict[str, dict] = {
    "top": {
        "url": TOP_HEADLINES_URL,
        "params": {"country": "us", "pageSize": 10},
    },
    "business": {
        "url": TOP_HEADLINES_URL,
        "params": {"country": "us", "category": "business", "pageSize": 10},
    },
    "technology": {
        "url": TOP_HEADLINES_URL,
        "params": {"country": "us", "category": "technology", "pageSize": 10},
    },
    "ai": {
        "url": EVERYTHING_URL,
        "params": {
            "q": '"artificial intelligence" OR "machine learning" OR "large language model" OR OpenAI OR Anthropic',
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 10,
        },
    },
    "investing": {
        "url": EVERYTHING_URL,
        "params": {
            "q": "stocks OR investing OR markets OR S&P OR nasdaq OR earnings",
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 10,
        },
    },
}


async def get_news(api_key: str, topic: str = "top", count: int = 8) -> str:
    """
    Fetch news headlines for the given topic.
    topic: "top", "ai", "business", "investing", "technology", or a custom search query.
    """
    if not api_key:
        return "News not available — NEWSAPI_KEY not configured."

    count = max(1, min(count, 20))
    topic_lower = topic.lower().strip()

    if topic_lower in TOPIC_CONFIGS:
        config = TOPIC_CONFIGS[topic_lower]
        url = config["url"]
        params = dict(config["params"])
        params["pageSize"] = count
        params["apiKey"] = api_key
        # For "everything" endpoint, limit to last 3 days
        if url == EVERYTHING_URL:
            from_date = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
            params["from"] = from_date
    else:
        # Custom search query
        url = EVERYTHING_URL
        params = {
            "q": topic,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": count,
            "apiKey": api_key,
            "from": (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"),
        }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    articles = data.get("articles", [])
    if not articles:
        return f"No news found for topic: {topic}"

    label = {
        "top": "Top Headlines",
        "ai": "AI & Machine Learning News",
        "business": "Business Headlines",
        "investing": "Markets & Investing News",
        "technology": "Tech Headlines",
    }.get(topic_lower, f'News: "{topic}"')

    lines = [f"{label}:"]
    for i, a in enumerate(articles[:count], 1):
        title = a.get("title", "No title").split(" - ")[0].strip()
        source = a.get("source", {}).get("name", "")
        desc = (a.get("description") or "").strip()
        url_str = a.get("url", "")
        line = f"{i}. {title}"
        if source:
            line += f" ({source})"
        if desc and len(desc) < 200:
            line += f"\n   {desc}"
        lines.append(line)

    return "\n".join(lines)
