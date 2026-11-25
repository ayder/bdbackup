"""Offline regression for the Docker SSH prerequisite fixture."""

from scripts import integration_ssh_copy


def test_probe_requests_umasks_that_sshd_sessions_can_produce():
    # Both Ubuntu fixtures start login sessions with at least 022.
    assert integration_ssh_copy.UMASKS == (0o022, 0o077), (
        "the fixture must request achievable session umasks"
    )
