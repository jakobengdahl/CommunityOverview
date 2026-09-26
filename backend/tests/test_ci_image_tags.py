"""Pins the branch each explicit floating image tag is enabled on.

Deployments pull the floating tags, not the immutable sha- tag. The explicit
`type=raw` lines enable `dev` on preview and `latest` on prod, in a
docker/metadata-action `tags:` block that no other check reads, so swapping
the two branches, or pointing `dev` at main, would publish the wrong build to
an environment with every test still green.

This pins the raw tag lines only. metadata-action's `flavor` can add `latest`
on its own (e.g. for a semver tag push), which is outside what is asserted here.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

FLOATING_TAGS = {
    "type=raw,value=dev,enable=${{ github.ref == 'refs/heads/preview' }}",
    "type=raw,value=latest,enable=${{ github.ref == 'refs/heads/prod' }}",
}


def _metadata_steps():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build"]["steps"]
    return [s for s in steps if s.get("uses", "").startswith("docker/metadata-action")]


def test_both_images_extract_tag_metadata():
    assert {s["id"] for s in _metadata_steps()} == {"meta-core", "meta-gateway"}


def test_raw_floating_tags_are_enabled_on_preview_and_prod_only():
    for step in _metadata_steps():
        lines = [
            line.strip()
            for line in step["with"]["tags"].splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        raw = {line for line in lines if line.startswith("type=raw")}
        assert raw == FLOATING_TAGS, f"{step['id']} floating tags drifted: {raw}"
