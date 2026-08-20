"""Send finished meeting notes to a Notion page.

Uses Notion's public REST API with a user-supplied *internal integration*
token - the same mechanism a Notion "connection" uses. (An MCP server is the
analogous plumbing for LLM clients like Claude; for an app pushing pages INTO
Notion, the REST API is the direct wire.)

Setup the user must do once, and the two errors it explains:
  1. Create an integration at notion.so/my-integrations and copy the secret
     ("ntn_..." / legacy "secret_..."). A wrong token -> 401.
  2. Share the target page with that integration (page ... menu -> Connections).
     A page that exists but was never shared -> 404 from Notion, which is why
     the 404 message here talks about sharing, not existence.

Pure stdlib + requests. No Qt, no project imports - unit-testable on its own.
"""
import re
import time

import requests

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"   # stable versioned API; newer versions are additive
MAX_RICH_LEN = 1800             # Notion caps rich_text content at 2000 chars
MAX_CHILDREN_PER_REQ = 100      # Notion caps children blocks per request


class NotionError(RuntimeError):
    pass


def extract_page_id(raw):
    """A Notion page id from a pasted URL or a bare id, dashed or not.

    Notion URLs end with a 32-hex-char id, usually glued to a title slug:
    https://www.notion.so/My-Meetings-0123456789abcdef0123456789abcdef
    Returns the dashed UUID form, or None when nothing id-shaped is present.
    """
    if not raw:
        return None
    # Drop query AND fragment: a URL copied after following an anchor looks
    # like .../Slug-<pageid>#<blockid>, and the block id would otherwise win
    # the last-match rule below.
    text = raw.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    candidates = re.findall(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        r"|[0-9a-fA-F]{32}",
        text)
    if not candidates:
        return None
    # A workspace URL can carry several id-shaped runs; the page id is the last.
    h = candidates[-1].replace("-", "").lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _rich(text):
    """Rich-text array, chunked under Notion's per-item length cap."""
    text = text or ""
    if not text:
        return []
    return [{"type": "text", "text": {"content": text[i:i + MAX_RICH_LEN]}}
            for i in range(0, len(text), MAX_RICH_LEN)]


def _block(block_type, text):
    return {"object": "block", "type": block_type,
            block_type: {"rich_text": _rich(text)}}


def markdown_to_blocks(md):
    """Translate the notes' markdown subset into Notion blocks.

    Handles what our summaries actually emit: ## / ### headings, - [ ] todos,
    -/* bullets, numbered items, and plain paragraphs. Anything else stays a
    paragraph - never dropped.
    """
    blocks = []
    for raw_line in (md or "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("### "):
            blocks.append(_block("heading_3", line[4:]))
        elif line.startswith("## "):
            blocks.append(_block("heading_2", line[3:]))
        elif line.startswith("# "):
            blocks.append(_block("heading_1", line[2:]))
        elif (m := re.match(r"^[-*] \[([ xX])\] (.*)$", line)):
            blocks.append({"object": "block", "type": "to_do",
                           "to_do": {"rich_text": _rich(m.group(2).strip()),
                                     "checked": m.group(1).lower() == "x"}})
        elif line.startswith(("- ", "* ")):
            blocks.append(_block("bulleted_list_item", line[2:]))
        elif re.match(r"^\d+[.)] ", line):
            blocks.append(_block("numbered_list_item",
                                 line.split(" ", 1)[1] if " " in line else line))
        else:
            blocks.append(_block("paragraph", line))
    return blocks


def transcript_blocks(transcript):
    """One paragraph per transcript line, under a Transcript heading."""
    if not (transcript or "").strip():
        return []
    blocks = [_block("heading_2", "Transcript")]
    for line in transcript.split("\n"):
        line = line.strip()
        if line:
            blocks.append(_block("paragraph", line))
    return blocks


def _headers(token):
    return {"Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"}


def _raise_for(resp):
    if resp.status_code == 401:
        raise NotionError("Notion rejected the token - re-copy the integration "
                          "secret from notion.so/my-integrations.")
    if resp.status_code == 404:
        raise NotionError("Notion can't see that page. Open the page in Notion "
                          "-> ⋯ menu -> Connections -> add your integration, "
                          "then try again.")
    if resp.status_code == 429:
        raise NotionError("Notion rate limit hit - wait a moment and try again.")
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = (resp.json().get("message") or "")[:160]
        except Exception:
            pass
        raise NotionError(f"Notion error {resp.status_code}: {detail or 'request failed'}")


def publish_meeting(token, parent_page, title, notes_md, transcript="", meta_line=""):
    """Create a Notion page under ``parent_page`` and return its URL.

    Never partial-fails silently: the page is created with the first batch of
    blocks, and remaining batches are appended; an append error still leaves a
    valid page, so the error message includes the URL that DID get created.
    """
    token = (token or "").strip()
    page_id = extract_page_id(parent_page)
    if not token:
        raise NotionError("Add your Notion integration token first.")
    if not page_id:
        raise NotionError("That doesn't look like a Notion page link or id.")

    blocks = []
    if meta_line:
        blocks.append(_block("paragraph", meta_line))
    blocks.extend(markdown_to_blocks(notes_md))
    blocks.extend(transcript_blocks(transcript))
    if not blocks:
        raise NotionError("Nothing to send yet - generate the notes first.")

    first, rest = blocks[:MAX_CHILDREN_PER_REQ], blocks[MAX_CHILDREN_PER_REQ:]
    try:
        resp = requests.post(
            f"{API_BASE}/pages",
            headers=_headers(token),
            json={
                "parent": {"page_id": page_id},
                "properties": {"title": {"title": _rich(title or "Meeting notes")}},
                "children": first,
            },
            timeout=30,
        )
    except requests.RequestException as e:
        raise NotionError(f"Could not reach Notion: {e}") from e
    _raise_for(resp)
    data = resp.json()
    new_id, url = data.get("id"), data.get("url", "")

    for i in range(0, len(rest), MAX_CHILDREN_PER_REQ):
        batch = rest[i:i + MAX_CHILDREN_PER_REQ]
        time.sleep(0.34)        # Notion allows ~3 requests/second
        try:
            r = requests.patch(
                f"{API_BASE}/blocks/{new_id}/children",
                headers=_headers(token),
                json={"children": batch},
                timeout=30,
            )
            _raise_for(r)
        except NotionError as e:
            raise NotionError(
                f"Page created ({url}) but part of the transcript failed to "
                f"upload: {e}") from e
        except requests.RequestException as e:
            raise NotionError(
                f"Page created ({url}) but part of the transcript failed to "
                f"upload: {e}") from e
    return url
