"""
SkillsLoader: Fetches and parses SKILL.md files from remote URLs.

Supports:
- agentskills.io standard format (YAML frontmatter + Markdown body)
- Claude Code extensions (when-to-use, allowed-tools, effort, etc.)
- Direct file URLs (raw.githubusercontent.com or any raw SKILL.md URL)
- GitHub repository URLs (auto-discovers skills in standard directories)
- Local filesystem directory (skills_dir config option)

Security:
- Content is sanitized against prompt injection patterns
- Dangerous HTML elements stripped; safe markup preserved
- Domain allowlist configurable via SkillsConfig
- Maximum content size enforced
- Failed fetches are logged and skipped without crashing
"""

import re
import json
import logging
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any
from enum import Enum

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Standard skill directory locations searched in GitHub repos (in priority order)
GITHUB_SKILL_DIRS = [
    ".agents/skills",
    ".claude/skills",
    ".github/skills",
    "skills",
]

# Prompt injection patterns — skills matching these are rejected outright
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"\bSYSTEM:\s",
    r"you\s+are\s+now\s+a",
    r"disregard\s+(your\s+)?(previous\s+)?instructions",
    r"new\s+instructions\s*:",
    r"override\s+(your\s+)?previous",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

# Dangerous HTML elements that may embed executable content or exfiltrate data
_DANGEROUS_TAG_RE = re.compile(
    r"<(script|iframe|style|form|link|object|embed|base|meta)[^>]*>.*?</\1>|"
    r"<(script|iframe|style|form|link|object|embed|base|meta)[^>]*/?>",
    re.IGNORECASE | re.DOTALL,
)

DEFAULT_MAX_CONTENT_BYTES = 50_000
DEFAULT_MAX_BODY_CHARS = 8_000


class SkillsConfig(BaseModel):
    """Configuration for the skills loading system."""
    skills_dir: str = "config/default/skills"
    allow_external_skills: bool = True
    trusted_domains: List[str] = Field(default_factory=lambda: [
        "github.com",
        "raw.githubusercontent.com",
        "agentskills.io",
        "api.github.com",
    ])
    cache_ttl_seconds: int = 3600
    max_skill_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES
    max_skill_body_chars: int = DEFAULT_MAX_BODY_CHARS
    github_token: Optional[str] = None


class SkillDefinition(BaseModel):
    """
    A parsed agent skill definition (SKILL.md format).

    Compatible with agentskills.io standard and Claude Code extensions.
    """
    id: str
    name: str
    description: str
    content: str                          # Markdown body — the actual prompt text
    allowed_tools: List[str] = Field(default_factory=list)
    when_to_use: Optional[str] = None     # Claude Code / agentskills.io extension
    effort: Optional[str] = None          # Claude Code extension
    license: Optional[str] = None
    metadata: Dict[str, str] = Field(default_factory=dict)
    source_url: str
    loaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_prompt_block(self) -> str:
        """Format skill for injection into an LLM system prompt."""
        parts = [f'<skill name="{self.name}">']
        if self.when_to_use:
            parts.append(f"When to use: {self.when_to_use}")
            parts.append("")
        if self.effort:
            parts.append(f"Effort level: {self.effort}")
            parts.append("")
        if self.description:
            parts.append(f"Description: {self.description}")
            parts.append("")
        if self.allowed_tools:
            parts.append(f"Expected tools: {', '.join(self.allowed_tools)}")
            parts.append("")
        parts.append(self.content)
        parts.append("</skill>")
        return "\n".join(parts)


class _UrlType(str, Enum):
    DIRECT_FILE = "direct_file"
    GITHUB_REPO = "github_repo"
    AGENTSKILLS_IO = "agentskills_io"


class SkillsLoader:
    """
    Loads SKILL.md skills from remote URLs and local directories.

    Usage:
        loader = SkillsLoader(config)
        skills = await loader.load_from_urls(["https://github.com/org/repo", ...])
    """

    def __init__(self, config: Optional[SkillsConfig] = None):
        self._config = config or SkillsConfig()
        # Cache: normalised_url -> (skills, loaded_at)
        self._cache: Dict[str, tuple[List[SkillDefinition], datetime]] = {}

    async def load_from_urls(self, urls: List[str]) -> List[SkillDefinition]:
        """
        Load skills from a list of URLs, with deduplication by skill ID.

        Failures are logged and skipped; the caller always gets a (possibly
        empty) list back.
        """
        results: List[SkillDefinition] = []
        seen_ids: set = set()

        for url in urls:
            try:
                skills = await self._load_single(url)
                for skill in skills:
                    if skill.id not in seen_ids:
                        seen_ids.add(skill.id)
                        results.append(skill)
                    else:
                        logger.debug("Skipping duplicate skill id=%s from %s", skill.id, url)
                logger.info("Loaded %d skill(s) from %s", len(skills), url)
            except Exception as exc:
                logger.warning("Failed to load skills from %s: %s", url, exc)

        return results

    async def load_from_dir(self, skills_dir: Optional[str] = None) -> List[SkillDefinition]:
        """
        Load skills from a local directory of SKILL.md files.

        Searches for SKILL.md files in <skills_dir>/<skill-name>/SKILL.md.
        Falls back to the configured skills_dir if no path is given.
        """
        dir_path = Path(skills_dir or self._config.skills_dir)
        if not dir_path.exists():
            logger.debug("Skills directory does not exist: %s", dir_path)
            return []

        results: List[SkillDefinition] = []
        for skill_md in sorted(dir_path.rglob("SKILL.md")):
            try:
                content = skill_md.read_text(encoding="utf-8")
                source_url = skill_md.as_uri()
                skill = self._parse_skill_md(content, source_url=source_url)
                if skill:
                    results.append(skill)
                    logger.debug("Loaded local skill: %s", skill.name)
            except Exception as exc:
                logger.warning("Failed to load local skill %s: %s", skill_md, exc)

        logger.info("Loaded %d local skill(s) from %s", len(results), dir_path)
        return results

    async def _load_single(self, url: str) -> List[SkillDefinition]:
        """Dispatch to the right loader based on URL type."""
        cache_key = _normalise_url(url)
        url_type = self._classify_url(cache_key)

        cached = self._cache.get(cache_key)
        if cached:
            skills, loaded_at = cached
            age = (datetime.now(timezone.utc) - loaded_at).total_seconds()
            if age < self._config.cache_ttl_seconds:
                return skills

        if url_type == _UrlType.GITHUB_REPO:
            skills = await self._load_github_repo(cache_key)
        elif url_type == _UrlType.AGENTSKILLS_IO:
            skills = await self._load_agentskills_io(cache_key)
        else:
            skills = await self._load_direct_file(cache_key)

        self._cache[cache_key] = (skills, datetime.now(timezone.utc))
        return skills

    def _classify_url(self, url: str) -> _UrlType:
        """Determine what kind of URL this is."""
        if "agentskills.io" in url and "/skills/" in url and not url.endswith("SKILL.md"):
            return _UrlType.AGENTSKILLS_IO
        if (
            "github.com" in url
            and "raw.githubusercontent.com" not in url
            and not url.endswith(".md")
        ):
            parts = url.rstrip("/").split("github.com/")[-1].split("/")
            if len(parts) <= 2:
                return _UrlType.GITHUB_REPO
        return _UrlType.DIRECT_FILE

    def _validate_domain(self, url: str) -> None:
        """Raise ValueError if the domain is not trusted."""
        if not self._config.allow_external_skills:
            raise ValueError(f"External skills are disabled (url: {url})")
        for domain in self._config.trusted_domains:
            if domain in url:
                return
        raise ValueError(f"Domain not in trusted_domains allowlist: {url}")

    async def _fetch_text(self, url: str, headers: Optional[Dict[str, str]] = None) -> str:
        """Fetch URL and return text, with size guard."""
        self._validate_domain(url)
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            response = await client.get(url, headers=headers or {})
            response.raise_for_status()
            content = response.text
            if len(content.encode()) > self._config.max_skill_content_bytes:
                raise ValueError(
                    f"Content from {url} exceeds max size "
                    f"({self._config.max_skill_content_bytes} bytes)"
                )
            return content

    def _github_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self._config.github_token:
            headers["Authorization"] = f"Bearer {self._config.github_token}"
        return headers

    async def _load_direct_file(self, url: str) -> List[SkillDefinition]:
        """Load a single SKILL.md file from a direct URL."""
        url = _github_blob_to_raw(url)
        text = await self._fetch_text(url)
        skill = self._parse_skill_md(text, source_url=url)
        if skill:
            return [skill]
        return []

    async def _load_github_repo(self, url: str) -> List[SkillDefinition]:
        """
        Discover and load all SKILL.md files from a GitHub repository.

        Searches standard skill directories in priority order.
        """
        owner_repo = _extract_github_owner_repo(url)
        if not owner_repo:
            raise ValueError(f"Cannot parse GitHub repo from URL: {url}")

        owner, repo = owner_repo
        tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"

        try:
            tree_json = await self._fetch_text(tree_url, headers=self._github_headers())
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"GitHub API error for {url}: {exc}") from exc

        tree = json.loads(tree_json)
        skill_paths = _find_skill_paths(tree.get("tree", []))

        if not skill_paths:
            logger.debug("No SKILL.md files found in %s/%s", owner, repo)
            return []

        skills: List[SkillDefinition] = []
        for path in skill_paths:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}"
            try:
                text = await self._fetch_text(raw_url)
                skill = self._parse_skill_md(text, source_url=raw_url)
                if skill:
                    skills.append(skill)
            except Exception as exc:
                logger.warning("Skipping %s in %s/%s: %s", path, owner, repo, exc)

        return skills

    async def _load_agentskills_io(self, url: str) -> List[SkillDefinition]:
        """
        Load a skill from agentskills.io.

        Attempts the raw SKILL.md first via embedded GitHub links; falls
        back to treating the page content itself as SKILL.md.
        Best-effort — if the site structure changes this may break.
        """
        try:
            page = await self._fetch_text(url)
            raw_links = re.findall(
                r'https://raw\.githubusercontent\.com/[^\s"\'<>]+SKILL\.md',
                page,
            )
            skills: List[SkillDefinition] = []
            for raw_url in raw_links:
                try:
                    text = await self._fetch_text(raw_url)
                    skill = self._parse_skill_md(text, source_url=raw_url)
                    if skill:
                        skills.append(skill)
                except Exception as exc:
                    logger.warning("agentskills.io: failed to fetch raw link %s: %s", raw_url, exc)
            if skills:
                return skills

            skill = self._parse_skill_md(page, source_url=url)
            if skill:
                return [skill]
        except Exception as exc:
            logger.warning("agentskills.io fetch failed for %s: %s", url, exc)

        return []

    def _parse_skill_md(self, text: str, source_url: str) -> Optional[SkillDefinition]:
        """
        Parse a SKILL.md file (YAML frontmatter + Markdown body).

        Returns None if the file cannot be parsed or fails safety checks.
        """
        text = text.strip()
        if not text.startswith("---"):
            # No frontmatter — treat entire content as plain skill body
            name = _name_from_url(source_url)
            sanitized = self._sanitize(text)
            if not sanitized:
                return None
            return SkillDefinition(
                id=_make_id(name),
                name=name,
                description="",
                content=sanitized[:self._config.max_skill_body_chars],
                source_url=source_url,
            )

        parts = text.split("---", 2)
        if len(parts) < 3:
            logger.debug("Malformed frontmatter in %s", source_url)
            return None

        frontmatter_text = parts[1].strip()
        body = parts[2].strip()

        try:
            fm = _parse_yaml_simple(frontmatter_text)
        except Exception as exc:
            logger.warning("YAML parse error in %s: %s", source_url, exc)
            return None

        name = fm.get("name", "").strip()
        description = fm.get("description", "").strip()

        if not name:
            logger.debug("Skipping skill in %s: missing required 'name' field", source_url)
            return None

        if not description:
            logger.debug("Skill '%s' in %s has no description field", name, source_url)

        # allowed-tools: spec says space-separated string; YAML may also produce a list
        allowed_tools_raw = fm.get("allowed-tools", fm.get("allowed_tools", ""))
        if isinstance(allowed_tools_raw, list):
            allowed_tools = [str(t).strip() for t in allowed_tools_raw if t]
        else:
            allowed_tools = str(allowed_tools_raw).split() if allowed_tools_raw else []

        raw_meta = fm.get("metadata", {})
        meta = {str(k): str(v) for k, v in raw_meta.items()} if isinstance(raw_meta, dict) else {}

        skill_id = meta.get("id") or _make_id(name)

        body_stripped = body.strip()
        sanitized_body = self._sanitize(body_stripped)
        # Reject the entire skill if a non-empty body was wiped by the sanitizer
        # (injection pattern detected — not just a skill with no body at all)
        if body_stripped and not sanitized_body:
            logger.warning("Skill '%s' from %s rejected: body contains injection pattern", name, source_url)
            return None
        if not sanitized_body and not description:
            logger.debug("Skipping skill '%s': no content", name)
            return None

        return SkillDefinition(
            id=skill_id,
            name=name,
            description=description,
            content=sanitized_body[:self._config.max_skill_body_chars],
            allowed_tools=allowed_tools,
            when_to_use=fm.get("when-to-use") or fm.get("when_to_use"),
            effort=fm.get("effort"),
            license=fm.get("license"),
            metadata=meta,
            source_url=source_url,
        )

    def _sanitize(self, text: str) -> str:
        """
        Prompt injection guard.

        Rejects skills containing known injection patterns.
        Strips only dangerous HTML elements (script, iframe, etc.) that
        could embed executable content; safe markup is preserved so that
        skill authors can use <example>, <code>, etc. in their content.
        """
        if _INJECTION_RE.search(text):
            logger.warning("Skill content rejected: contains prompt injection pattern")
            return ""
        cleaned = _DANGEROUS_TAG_RE.sub("", text)
        return cleaned.strip()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _normalise_url(url: str) -> str:
    """Normalise a URL for use as a cache key (strip trailing slashes)."""
    return url.rstrip("/")


def _github_blob_to_raw(url: str) -> str:
    """Convert a github.com/blob/ URL to raw.githubusercontent.com."""
    return url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")


def _extract_github_owner_repo(url: str) -> Optional[tuple[str, str]]:
    """Extract (owner, repo) from a github.com URL."""
    match = re.search(r"github\.com/([^/]+)/([^/?#]+)", url)
    if match:
        return match.group(1), match.group(2).removesuffix(".git")
    return None


def _find_skill_paths(tree_items: List[Dict[str, Any]]) -> List[str]:
    """
    Find all SKILL.md paths in a GitHub tree that live inside known skill dirs.
    """
    paths = []
    for item in tree_items:
        path: str = item.get("path", "")
        if not path.endswith("SKILL.md"):
            continue
        for skill_dir in GITHUB_SKILL_DIRS:
            if path.startswith(skill_dir + "/") or path == skill_dir + "/SKILL.md":
                paths.append(path)
                break
    return paths


def _name_from_url(url: str) -> str:
    """Derive a human-readable skill name from the source URL."""
    last = url.rstrip("/").split("/")[-1]
    name = re.sub(r"[_-]+", " ", last)
    name = re.sub(r"\.(md|json)$", "", name, flags=re.IGNORECASE)
    return name.title() or "Unknown Skill"


def _make_id(name: str) -> str:
    """Create a stable lowercase-hyphenated ID from a name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or hashlib.md5(name.encode()).hexdigest()[:8]


def _parse_yaml_simple(text: str) -> Dict[str, Any]:
    """
    Minimal YAML parser for SKILL.md frontmatter.

    Uses PyYAML when available; falls back to line-by-line parsing for the
    simple subset used in practice (scalar values and one-level nested maps).
    PyYAML handles lists and complex types correctly; the fallback does not
    support YAML list syntax.
    """
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        pass

    result: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_map: Optional[Dict[str, str]] = None

    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if ":" in stripped:
            k, _, v = stripped.partition(":")
            k = k.strip()
            v = v.strip().strip('"').strip("'")

            if indent == 0:
                current_key = k
                if v:
                    result[k] = v
                    current_map = None
                else:
                    result[k] = {}
                    current_map = result[k]
            elif indent > 0 and current_map is not None:
                current_map[k] = v

    return result
