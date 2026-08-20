"""Notion export: id parsing, markdown->blocks, and the publish flow (mocked).

Pure stdlib + mock - no network, no Qt.
"""
import unittest
from unittest.mock import MagicMock, patch

import notion_export as ne


class TestExtractPageId(unittest.TestCase):
    DASHED = "01234567-89ab-cdef-0123-456789abcdef"

    def test_url_with_slug(self):
        url = "https://www.notion.so/My-Meetings-0123456789abcdef0123456789abcdef"
        self.assertEqual(ne.extract_page_id(url), self.DASHED)

    def test_url_with_query(self):
        url = ("https://www.notion.so/ws/My-Meetings-0123456789abcdef0123456789abcdef"
               "?pvs=4")
        self.assertEqual(ne.extract_page_id(url), self.DASHED)

    def test_bare_ids(self):
        self.assertEqual(ne.extract_page_id("0123456789abcdef0123456789abcdef"),
                         self.DASHED)
        self.assertEqual(ne.extract_page_id(self.DASHED), self.DASHED)

    def test_last_id_wins(self):
        # Workspace URLs can carry more than one 32-hex run; the page id is last.
        url = ("https://www.notion.so/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
               "Page-0123456789abcdef0123456789abcdef")
        self.assertEqual(ne.extract_page_id(url), self.DASHED)

    def test_fragment_block_anchor_resolves_to_page_id(self):
        # A URL copied after following an anchor carries the BLOCK id in the
        # fragment; the page id (before the #) must win.
        url = ("https://www.notion.so/My-Meetings-0123456789abcdef0123456789abcdef"
               "#a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
        self.assertEqual(ne.extract_page_id(url), self.DASHED)

    def test_garbage(self):
        self.assertIsNone(ne.extract_page_id("not a notion link"))
        self.assertIsNone(ne.extract_page_id(""))
        self.assertIsNone(ne.extract_page_id(None))


class TestMarkdownToBlocks(unittest.TestCase):
    def test_headings_bullets_todos(self):
        md = "## Summary\nThe team met.\n### Details\n- point one\n- [ ] task A\n- [x] done B\n1. first"
        blocks = ne.markdown_to_blocks(md)
        kinds = [b["type"] for b in blocks]
        self.assertEqual(kinds, ["heading_2", "paragraph", "heading_3",
                                 "bulleted_list_item", "to_do", "to_do",
                                 "numbered_list_item"])
        self.assertFalse(blocks[4]["to_do"]["checked"])
        self.assertTrue(blocks[5]["to_do"]["checked"])

    def test_long_text_chunked_under_notion_cap(self):
        blocks = ne.markdown_to_blocks("x" * 4000)
        rich = blocks[0]["paragraph"]["rich_text"]
        self.assertGreater(len(rich), 1)
        self.assertTrue(all(len(r["text"]["content"]) <= ne.MAX_RICH_LEN for r in rich))

    def test_nothing_dropped(self):
        md = "weird |table| line\n> quote"
        blocks = ne.markdown_to_blocks(md)
        self.assertEqual(len(blocks), 2)     # both survive as paragraphs

    def test_empty(self):
        self.assertEqual(ne.markdown_to_blocks(""), [])
        self.assertEqual(ne.markdown_to_blocks(None), [])


class TestPublishFlow(unittest.TestCase):
    TOKEN = "ntn_test"
    PAGE = "0123456789abcdef0123456789abcdef"

    def _resp(self, status=200, body=None):
        r = MagicMock(status_code=status)
        r.json.return_value = body or {"id": "new-page-id",
                                       "url": "https://notion.so/new-page"}
        return r

    def test_small_page_single_request(self):
        with patch.object(ne.requests, "post", return_value=self._resp()) as post, \
             patch.object(ne.requests, "patch") as patch_req:
            url = ne.publish_meeting(self.TOKEN, self.PAGE, "Standup",
                                     "## Summary\nWe met.")
        self.assertEqual(url, "https://notion.so/new-page")
        post.assert_called_once()
        patch_req.assert_not_called()
        body = post.call_args[1]["json"]
        self.assertEqual(body["parent"]["page_id"],
                         ne.extract_page_id(self.PAGE))
        self.assertLessEqual(len(body["children"]), ne.MAX_CHILDREN_PER_REQ)

    def test_long_transcript_batches(self):
        transcript = "\n".join(f"Speaker 1: line {i}" for i in range(250))
        with patch.object(ne.requests, "post", return_value=self._resp()) as post, \
             patch.object(ne.requests, "patch", return_value=self._resp()) as patch_req, \
             patch.object(ne.time, "sleep"):
            ne.publish_meeting(self.TOKEN, self.PAGE, "Long", "## S\nnotes",
                               transcript=transcript)
        # 3 notes blocks + 251 transcript blocks = 254 -> 100 create + 2 appends
        self.assertEqual(post.call_count, 1)
        self.assertEqual(patch_req.call_count, 2)
        for c in patch_req.call_args_list:
            self.assertLessEqual(len(c[1]["json"]["children"]),
                                 ne.MAX_CHILDREN_PER_REQ)

    def test_401_explains_token(self):
        with patch.object(ne.requests, "post", return_value=self._resp(401)):
            with self.assertRaises(ne.NotionError) as ctx:
                ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes")
        self.assertIn("token", str(ctx.exception).lower())

    def test_404_explains_sharing(self):
        # The classic Notion gotcha: page exists but wasn't shared with the
        # integration - Notion returns 404, users think the id is wrong.
        with patch.object(ne.requests, "post", return_value=self._resp(404)):
            with self.assertRaises(ne.NotionError) as ctx:
                ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes")
        self.assertIn("Connections", str(ctx.exception))

    def test_missing_config(self):
        with self.assertRaises(ne.NotionError):
            ne.publish_meeting("", self.PAGE, "T", "notes")
        with self.assertRaises(ne.NotionError):
            ne.publish_meeting(self.TOKEN, "garbage", "T", "notes")


if __name__ == "__main__":
    unittest.main()
