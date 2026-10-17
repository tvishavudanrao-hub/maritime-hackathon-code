"""News tailored to the fleet and the route: which regions to search, how to tag and rank what comes back.

Pure functions, no network. server.py fetches the headlines; this module only builds the query and ranks.
Headlines stay advisory: a tag or a rank never closes a corridor.
"""
from __future__ import annotations

import re

# zone id (as in data/network.json) -> search terms and the words that put a headline "in" that region
REGIONS = {
    'hormuz': {'terms': ('Strait of Hormuz',), 'words': ('hormuz', 'persian gulf')},
    'bab': {'terms': ('Bab el Mandeb', 'Red Sea'), 'words': ('bab el', 'red sea', 'houthi', 'yemen')},
    'suez': {'terms': ('Suez Canal',), 'words': ('suez', 'canal')},
    'malacca': {'terms': ('Strait of Malacca', 'Singapore Strait'), 'words': ('malacca', 'singapore strait')},
    'cape': {'terms': ('Cape of Good Hope',), 'words': ('cape of good hope', 'cape town', 'south africa')},
    'gulf_guinea': {'terms': ('Gulf of Guinea',), 'words': ('gulf of guinea', 'west africa')},
}
DEFAULT_REGIONS = ('hormuz', 'bab', 'suez')   # what the feed covered before it followed the route
DISRUPTION_WORDS = ('closed', 'closure', 'attack', 'attacked', 'missile', 'blockade', 'disrupt', 'strike', 'seized', 'threat')


def normalise_regions(regions) -> tuple:
    """Known regions only, in a fixed order, no duplicates. Nothing valid -> the default set."""
    wanted = {r for r in (regions or ()) if r in REGIONS}
    ordered = tuple(r for r in REGIONS if r in wanted)
    return ordered or DEFAULT_REGIONS


def region_terms(regions) -> str:
    # one flat OR list: Google News RSS returns nothing when the groups are nested
    return '(' + ' OR '.join(f'"{t}"' for r in normalise_regions(regions) for t in REGIONS[r]['terms']) + ')'


def build_query(regions, extra: str) -> str:
    """Route regions AND the extra terms (general shipping words, or the fleet's own terms)."""
    return region_terms(regions) + ' ' + extra


def cache_key(cargo, regions) -> str:
    return 'news/' + (cargo or '-') + '/' + ','.join(normalise_regions(regions))


def topic_for(title: str) -> str:
    t = title.lower()
    for zone in REGIONS:
        if any(w in t for w in REGIONS[zone]['words']):
            return zone
    return 'general'


def tag_and_rank(articles, seg, regions) -> list:
    """Add topic / potentialDisruption / relevant (fleet) / onRoute / tag, then rank:
    fleet + route first, then fleet, then route, then the rest. Stable within a group."""
    regions = normalise_regions(regions)
    # keywords match at the start of a word ('tanker' finds 'tankers', 'ore' does not find 'Singapore')
    fleet_re = re.compile(r'\b(?:' + '|'.join(re.escape(k) for k in seg['keywords']) + ')') if seg else None
    out = []
    for a in articles:
        title = a['title'].lower()
        topic = topic_for(a['title'])
        fleet = bool(fleet_re and fleet_re.search(title))
        route = topic in regions
        out.append({**a, 'topic': topic, 'potentialDisruption': any(w in title for w in DISRUPTION_WORDS),
                    'relevant': fleet, 'onRoute': route,
                    'tag': 'Your fleet' if fleet else 'On your route' if route else 'General'})
    out.sort(key=lambda a: -(2 * a['relevant'] + a['onRoute'] + a['relevant'] * a['onRoute']))   # 3, 2, 1, 0
    return out
