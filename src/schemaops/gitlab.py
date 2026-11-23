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

    def _call(self, suffix: str, method: str = "GET", payload: dict[str, str] | None = None) -> Any:
        if not (self.token and self.api_url and self.project_id):
            raise GovernanceError(
                "SCHEMAOPS_GITLAB_TOKEN (a project access token) is required to read merge request "
                "labels and approvals and to post the plan."
            )
        url = f"{self.api_url}/projects/{self.project_id}/merge_requests/{self.iid}{suffix}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"PRIVATE-TOKEN": self.token, "Content-Type": "application/json"}
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - GitLab API URL
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise GovernanceError(f"GitLab API request failed: {log.redact(str(exc))}") from exc

    def _get(self, suffix: str) -> dict[str, Any]:
        data = self._call(suffix)
        return data if isinstance(data, dict) else {}

    def post_comment(self, body: str, marker: str) -> bool:
        """Create the plan comment, or update the one a previous run left. Never raises."""
        try:
            notes = self._call("/notes?per_page=100&sort=desc")
            existing = next(
                (note for note in notes if isinstance(note, dict) and marker in str(note.get("body", ""))), None
            ) if isinstance(notes, list) else None
            if existing:
                self._call(f"/notes/{existing['id']}", "PUT", {"body": body})
            else:
                self._call("/notes", "POST", {"body": body})
            return True
        except GovernanceError as exc:
            logger.warning("could not post the plan to the merge request", extra={"reason": str(exc)})
            return False

    def approvers(self) -> set[str]:
        """User names of everyone who has approved the merge request."""
        approved = self._get("/approvals").get("approved_by") or []
        return {str(entry.get("user", {}).get("username", "")) for entry in approved if isinstance(entry, dict)}
