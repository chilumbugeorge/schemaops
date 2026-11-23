"""What schemaops needs to know about the GitLab merge request it runs in."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from schemaops import log
from schemaops.errors import GovernanceError

logger = log.get("gitlab")


@dataclass(frozen=True)
class MergeRequest:
    iid: str
    base_sha: str
    labels: frozenset[str]
    api_url: str = ""
    project_id: str = ""
    token: str = ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> MergeRequest | None:
        env = os.environ if environ is None else environ
        if not env.get("CI_MERGE_REQUEST_IID"):
            return None
        token = env.get("SCHEMAOPS_GITLAB_TOKEN", "")
        log.register_secret(token)
        labels = {label.strip() for label in env.get("CI_MERGE_REQUEST_LABELS", "").split(",") if label.strip()}
        request = cls(
            iid=env["CI_MERGE_REQUEST_IID"],
            base_sha=env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA", ""),
            labels=frozenset(labels),
            api_url=env.get("CI_API_V4_URL", ""),
            project_id=env.get("CI_PROJECT_ID", ""),
            token=token,
        )
        if token:
            # CI_MERGE_REQUEST_LABELS is fixed when the pipeline starts; ask the API for the live set.
            live = request._get("").get("labels")
            if isinstance(live, list):
                request = cls(**{**request.__dict__, "labels": frozenset(str(label) for label in live)})
        return request

    def _get(self, suffix: str) -> dict[str, Any]:
        if not (self.token and self.api_url and self.project_id):
            raise GovernanceError(
                "SCHEMAOPS_GITLAB_TOKEN (a project access token with read_api scope) is required "
                "to read merge request approvals."
            )
        url = f"{self.api_url}/projects/{self.project_id}/merge_requests/{self.iid}{suffix}"
        request = urllib.request.Request(url, headers={"PRIVATE-TOKEN": self.token})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - GitLab API URL
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise GovernanceError(f"GitLab API request failed: {log.redact(str(exc))}") from exc
        return data if isinstance(data, dict) else {}

    def approvers(self) -> set[str]:
        """User names of everyone who has approved the merge request."""
        approved = self._get("/approvals").get("approved_by") or []
        return {str(entry.get("user", {}).get("username", "")) for entry in approved if isinstance(entry, dict)}
