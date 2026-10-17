"""News tailored to the fleet and the route: query builder, topic detection, tags and ranking (no network)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_rank  # noqa: E402
from optimizer import CARGO_SEGMENTS  # noqa: E402


def art(title, url=None):
    return {'title': title, 'url': url or 'https://example.test/' + title.replace(' ', '-'), 'source': 'test', 'published': ''}


class RegionsAndQuery(unittest.TestCase):
    def test_default_regions_when_none_given(self):
        self.assertEqual(news_rank.normalise_regions(None), news_rank.DEFAULT_REGIONS)
        self.assertEqual(news_rank.normalise_regions([]), news_rank.DEFAULT_REGIONS)

    def test_unknown_regions_are_dropped_and_order_is_stable(self):
        self.assertEqual(news_rank.normalise_regions(['suez', 'bogus', 'bab', 'suez']), ('bab', 'suez'))
        self.assertEqual(news_rank.normalise_regions(['bogus']), news_rank.DEFAULT_REGIONS)

    def test_query_names_only_the_route_regions(self):
        q = news_rank.build_query(('malacca', 'cape'), CARGO_SEGMENTS['bulk']['news_terms'])
        self.assertIn('Strait of Malacca', q)
        self.assertIn('Cape of Good Hope', q)
        self.assertNotIn('Hormuz', q)
        self.assertIn('iron ore', q)

    def test_region_terms_are_one_flat_or_list(self):
        # Google News RSS returns nothing for nested parentheses, so the region part must be a single flat group.
        q = news_rank.region_terms(('bab', 'suez', 'malacca'))
        self.assertEqual(q.count('('), 1)
        self.assertEqual(q.count(')'), 1)
        self.assertEqual(q, '("Bab el Mandeb" OR "Red Sea" OR "Suez Canal" OR "Strait of Malacca" OR "Singapore Strait")')

    def test_cache_key_differs_by_fleet_and_route(self):
        a = news_rank.cache_key('crude', ('bab', 'suez'))
        self.assertNotEqual(a, news_rank.cache_key('retail', ('bab', 'suez')))
        self.assertNotEqual(a, news_rank.cache_key('crude', ('cape',)))
        self.assertEqual(a, news_rank.cache_key('crude', ['suez', 'bab']))   # order does not matter


class Topics(unittest.TestCase):
    def test_original_chokepoint_topics_unchanged(self):
        self.assertEqual(news_rank.topic_for('Missile hits ship near Hormuz'), 'hormuz')
        self.assertEqual(news_rank.topic_for('Houthi attack in the Red Sea'), 'bab')
        self.assertEqual(news_rank.topic_for('Suez Canal traffic recovers'), 'suez')
        self.assertEqual(news_rank.topic_for('Port strike in Rotterdam'), 'general')

    def test_new_route_regions_are_recognised(self):
        self.assertEqual(news_rank.topic_for('Piracy alert in the Strait of Malacca'), 'malacca')
        self.assertEqual(news_rank.topic_for('Storm closes ports near the Cape of Good Hope'), 'cape')
        self.assertEqual(news_rank.topic_for('Gulf of Guinea kidnapping report'), 'gulf_guinea')


class TagsAndRanking(unittest.TestCase):
    def setUp(self):
        self.tanker = CARGO_SEGMENTS['crude']
        self.items = [
            art('Port strike in Rotterdam'),                                  # general
            art('Houthi attack in the Red Sea disrupts liner traffic'),       # on route only
            art('VLCC rates jump as refinery demand rises'),                  # fleet only
            art('Tanker attacked near Bab el-Mandeb'),                        # fleet and on route
        ]
        self.out = news_rank.tag_and_rank(self.items, self.tanker, ('bab', 'suez'))

    def test_tags(self):
        tags = {a['title']: a['tag'] for a in self.out}
        self.assertEqual(tags['Tanker attacked near Bab el-Mandeb'], 'Your fleet')
        self.assertEqual(tags['VLCC rates jump as refinery demand rises'], 'Your fleet')
        self.assertEqual(tags['Houthi attack in the Red Sea disrupts liner traffic'], 'On your route')
        self.assertEqual(tags['Port strike in Rotterdam'], 'General')

    def test_on_route_flag_and_relevant_flag(self):
        both = next(a for a in self.out if a['title'].startswith('Tanker attacked'))
        self.assertTrue(both['onRoute'] and both['relevant'])
        fleet_only = next(a for a in self.out if a['title'].startswith('VLCC'))
        self.assertTrue(fleet_only['relevant'] and not fleet_only['onRoute'])

    def test_ranking_both_then_fleet_then_route_then_general(self):
        order = [a['title'] for a in self.out]
        self.assertEqual(order, ['Tanker attacked near Bab el-Mandeb',
                                 'VLCC rates jump as refinery demand rises',
                                 'Houthi attack in the Red Sea disrupts liner traffic',
                                 'Port strike in Rotterdam'])

    def test_ranking_is_stable_within_a_group(self):
        items = [art('Red Sea one'), art('Red Sea two'), art('Red Sea three')]
        out = news_rank.tag_and_rank(items, self.tanker, ('bab',))
        self.assertEqual([a['title'] for a in out], ['Red Sea one', 'Red Sea two', 'Red Sea three'])

    def test_switching_fleet_type_changes_tags(self):
        out = news_rank.tag_and_rank(self.items, CARGO_SEGMENTS['retail'], ('bab', 'suez'))
        tags = {a['title']: a['tag'] for a in out}
        self.assertEqual(tags['VLCC rates jump as refinery demand rises'], 'General')
        self.assertEqual(tags['Houthi attack in the Red Sea disrupts liner traffic'], 'Your fleet')   # 'liner'

    def test_no_fleet_segment_still_tags_route_news(self):
        out = news_rank.tag_and_rank(self.items, None, ('bab',))
        self.assertTrue(all(a['tag'] in ('On your route', 'General') for a in out))

    def test_keywords_match_word_starts_not_the_middle_of_words(self):
        bulk = CARGO_SEGMENTS['bulk']          # has the keyword 'ore'
        out = news_rank.tag_and_rank([art('PetroChina expands bunkering in Singapore'), art('Iron ore carrier delayed'),
                                      art('Tankers queue as oil prices spoil outlook')], bulk, ('malacca',))
        tags = {a['title']: a['relevant'] for a in out}
        self.assertFalse(tags['PetroChina expands bunkering in Singapore'])    # 'ore' inside 'Singapore'
        self.assertTrue(tags['Iron ore carrier delayed'])
        crude = news_rank.tag_and_rank([art('Tankers queue as prices rise'), art('Prices spoil the outlook')],
                                       CARGO_SEGMENTS['crude'], ('bab',))
        flags = {a['title']: a['relevant'] for a in crude}
        self.assertTrue(flags['Tankers queue as prices rise'])                  # plural still matches 'tanker'
        self.assertFalse(flags['Prices spoil the outlook'])                     # 'oil' inside 'spoil'

    def test_disruption_flag_is_kept(self):
        a = next(a for a in self.out if a['title'].startswith('Tanker attacked'))
        self.assertTrue(a['potentialDisruption'])
        b = next(a for a in self.out if a['title'].startswith('VLCC'))
        self.assertFalse(b['potentialDisruption'])


if __name__ == '__main__':
    unittest.main()
