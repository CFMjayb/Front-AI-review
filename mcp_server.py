"""EDOM Front MCP server — exposes Front API as tools callable from Claude Code.

Run:  python mcp_server.py
Register in ~/.claude/settings.json:
  {
    "mcpServers": {
      "edom-front": {
        "command": "python",
        "args": ["C:\\dev\\edom-email-ops\\mcp_server.py"],
        "env": {}
      }
    }
  }
"""
import os
from dotenv import load_dotenv
load_dotenv(override=True)

from mcp.server.fastmcp import FastMCP
from auth import get_front_api_token
from front_client import FrontClient

mcp = FastMCP("edom-front")


def _front() -> FrontClient:
    return FrontClient(get_front_api_token())


# ── Read tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def front_list_inboxes() -> list:
    """List all Front inboxes accessible to the EDOM token."""
    return _front().list_inboxes()


@mcp.tool()
def front_list_channels() -> list:
    """List all Front channels (email addresses)."""
    return _front().list_channels()


@mcp.tool()
def front_list_teammates(email: str = "") -> list:
    """List Front teammates. Pass email to filter by address."""
    return _front().list_teammates(email=email or None)


@mcp.tool()
def front_list_inbox_conversations(inbox_id: str, status: str = "open",
                                   since_ms: int = 0, limit: int = 50) -> list:
    """List conversations in a Front inbox. status: open|archived|deleted|spam."""
    return _front().list_inbox_conversations(
        inbox_id, status=status or None,
        since_ms=since_ms or None, limit=limit,
    )


@mcp.tool()
def front_list_assigned_conversations(teammate_id: str, since_ms: int = 0) -> list:
    """List conversations assigned to a teammate."""
    return _front().list_assigned_conversations(teammate_id, since_ms=since_ms or None)


@mcp.tool()
def front_get_conversation(conversation_id: str) -> dict:
    """Get a single conversation's metadata."""
    return _front().get_conversation(conversation_id)


@mcp.tool()
def front_get_conversation_messages(conversation_id: str) -> list:
    """Get all messages in a conversation."""
    return _front().get_conversation_messages(conversation_id)


@mcp.tool()
def front_get_conversation_comments(conversation_id: str) -> list:
    """Get all internal comments on a conversation."""
    return _front().get_conversation_comments(conversation_id)


@mcp.tool()
def front_list_conversation_tags(conversation_id: str) -> list:
    """List tags currently applied to a conversation."""
    return _front().list_conversation_tags(conversation_id)


@mcp.tool()
def front_search_conversations(query: str) -> list:
    """Search conversations by Front query string."""
    return _front().search_conversations(query)


@mcp.tool()
def front_list_tags() -> list:
    """List all tags in the Front workspace."""
    return _front().list_tags()


# ── Write tools ──────────────────────────────────────────────────────────────

@mcp.tool()
def front_add_comment(conversation_id: str, body: str) -> dict:
    """Add an internal comment to a conversation."""
    return _front().add_comment(conversation_id, body)


@mcp.tool()
def front_add_tag(conversation_id: str, tag_name: str) -> dict:
    """Add a tag to a conversation by name (creates the tag if it does not exist)."""
    return _front().add_tag(conversation_id, tag_name)


@mcp.tool()
def front_remove_tag(conversation_id: str, tag_name: str) -> bool:
    """Remove a tag from a conversation by name."""
    return _front().remove_tag(conversation_id, tag_name)


@mcp.tool()
def front_set_status(conversation_id: str, status: str) -> dict:
    """Set conversation status: open | archived | deleted | spam."""
    return _front().set_status(conversation_id, status)


@mcp.tool()
def front_set_assignee(conversation_id: str, teammate_id: str) -> dict:
    """Set a conversation's assignee to a teammate ID."""
    return _front().set_assignee(conversation_id, teammate_id)


@mcp.tool()
def front_create_draft(conversation_id: str, subject: str, body: str,
                       to: list[str]) -> dict:
    """Create a draft reply attached to a conversation. Saved for human review — never auto-sent."""
    return _front().create_draft(conversation_id, subject=subject, body=body, to=to)


if __name__ == "__main__":
    mcp.run()
