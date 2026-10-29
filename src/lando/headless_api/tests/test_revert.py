from unittest import mock

import pytest
from pydantic import ValidationError

from lando.environments import Environment
from lando.headless_api.api import RevertAction, resolve_action
from lando.headless_api.models.automation_job import AutomationAction, AutomationJob
from lando.main.models import JobStatus
from lando.main.scm import SCMType
from lando.main.scm.exceptions import PatchConflict


def revert_action(**kwargs):
    return {
        "action": "revert",
        "commit": "a" * 40,
        "reason": "Startup failure",
        **kwargs,
    }


@pytest.mark.parametrize(
    "data",
    [
        {"commit": "HEAD"},
        {"commit": "a" * 39},
        {"commit": "a" * 40 + "\n"},
        {"commit": "--help"},
        {"commit": "a" * 40 + "..HEAD"},
        {"reason": "  "},
        {"reason": "bad\x00reason"},
        {"reason": None},
    ],
)
def test_revert_validation(data):
    with pytest.raises(ValidationError):
        resolve_action(revert_action(**data))


def test_revert_requires_reason():
    data = revert_action()
    del data["reason"]
    with pytest.raises(ValidationError):
        resolve_action(data)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "environment,repo_name,email,enabled,expected",
    [
        (Environment.production, "firefox-autoland", "hackbot@mozilla.tld", True, 202),
        (
            Environment.staging,
            "staging-firefox-autoland",
            "hackbot@mozilla.tld",
            True,
            202,
        ),
        (Environment.development, "ff-test-dev", "hackbot@mozilla.tld", True, 202),
        (Environment.production, "firefox-autoland", "other@example.org", True, 403),
        (Environment.production, "ff-test-dev", "hackbot@mozilla.tld", True, 403),
        (Environment.staging, "firefox-autoland", "hackbot@mozilla.tld", True, 403),
        (Environment.development, "firefox-autoland", "hackbot@mozilla.tld", True, 403),
        (Environment.local, "firefox-autoland", "hackbot@mozilla.tld", True, 403),
        (Environment.test, "firefox-autoland", "hackbot@mozilla.tld", True, 403),
        (Environment.production, "firefox-autoland", "hackbot@mozilla.tld", False, 400),
    ],
)
def test_revert_authorization(
    client, headless_user, settings, environment, repo_name, email, enabled, expected
):
    from lando.main.models import Repo

    settings.ENVIRONMENT = environment
    user, token = headless_user
    user.email = email
    user.save()
    # Hackbot needs no general automation/direct-push permission.
    if email == "hackbot@mozilla.tld":
        user.user_permissions.clear()
    Repo.objects.create(
        name=repo_name, scm_type=SCMType.GIT, automation_enabled=enabled
    )
    response = client.post(
        f"/api/repo/{repo_name}",
        data={"actions": [revert_action()]},
        content_type="application/json",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == expected, response.content
    assert AutomationJob.objects.count() == (expected == 202)
    if expected == 202:
        job = AutomationJob.objects.get()
        assert job.requester_email == email
        assert job.actions.get().data == revert_action()
        assert (
            client.get(
                f"/api/job/{job.id}", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "operation",
    [
        {"actions": [revert_action(), revert_action()]},
        {"actions": [revert_action(), {"action": "tag", "name": "bad"}]},
        {"actions": [{"action": "tag", "name": "bad"}, revert_action()]},
        {"actions": [revert_action()], "relbranch": {"branch_name": "other"}},
    ],
)
def test_revert_rejects_other_operations(client, headless_user, settings, operation):
    from lando.main.models import Repo

    settings.ENVIRONMENT = Environment.production
    user, token = headless_user
    user.email = "hackbot@mozilla.tld"
    user.save()
    Repo.objects.create(
        name="firefox-autoland", scm_type=SCMType.GIT, automation_enabled=True
    )
    response = client.post(
        "/api/repo/firefox-autoland",
        data=operation,
        content_type="application/json",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    assert not AutomationJob.objects.exists()


@pytest.mark.parametrize("error", [PatchConflict("conflict"), ValueError("invalid")])
def test_revert_action_failure(error):
    from lando.headless_api.api import AutomationActionException

    scm = mock.Mock()
    scm.revert_commit.side_effect = error
    with pytest.raises(AutomationActionException) as raised:
        RevertAction(**revert_action()).process(mock.Mock(), mock.Mock(), scm, 0)
    assert not raised.value.is_fatal  # Worker returns True for a completed failure.
    assert "Startup failure" not in raised.value.message


@pytest.mark.django_db
def test_revert_never_skips_checks():
    from lando.api.legacy.workers.automation_worker import AutomationWorker

    job = AutomationJob.objects.create(status=JobStatus.SUBMITTED)
    AutomationAction.objects.create(
        job_id=job,
        action_type="revert",
        data=revert_action(reason="a=release"),
        order=0,
    )
    assert not AutomationWorker.skip_checks(None, job, [mock.Mock(desc="a=release")])
