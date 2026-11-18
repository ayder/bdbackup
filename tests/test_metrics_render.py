"""The family registry and the textfile rendering."""

from __future__ import annotations

import pytest

from bdbackup.metrics.registry import Family, Registry, engine_prefix
from bdbackup.metrics.render import Sample, render

EXIT = "bdbackup_job_last_run_exit_code"
SUCCESS = "bdbackup_metrics_success"
RESULT = "bdbackup_job_last_run_result"


def make_registry() -> Registry:
    registry = Registry()
    registry.declare(EXIT, "Exit code of the job's last run.", ("job_name", "job_type"))
    registry.declare(SUCCESS, "1 when every source was read.")
    registry.declare(RESULT, "The result of the job's last run; the value is 1.",
                     ("job_name", "job_type", "result"))
    registry.declare("bdbackup_unused", "Declared, with no sample.", ("job_name",))
    return registry


def sample(value, **labels) -> Sample:
    return Sample(EXIT, value, labels or {"job_name": "pg", "job_type": "pgbackrest"})


# ---- declaring families ---------------------------------------------------------------------


def test_a_family_is_declared_with_its_help_and_labels():
    registry = Registry()

    family = registry.declare(EXIT, "Exit code.", ("job_name", "job_type"))

    assert family == Family(EXIT, "Exit code.", ("job_name", "job_type"))
    assert registry.get(EXIT) == family
    assert registry.get("bdbackup_other") is None


def test_a_family_is_declared_once():
    registry = Registry()
    registry.declare(EXIT, "Exit code.", ("job_name",))

    with pytest.raises(ValueError, match="already declared"):
        registry.declare(EXIT, "Exit code.", ("job_name",))


@pytest.mark.parametrize("name", ["", "1bad", "has-dash", "has space", "bdbackup.dot"])
def test_a_metric_name_must_be_a_prometheus_name(name):
    with pytest.raises(ValueError, match="metric name"):
        Registry().declare(name, "Help.")


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        (("bad-label",), "label name"),
        (("1bad",), "label name"),
        (("__reserved",), "label name"),
        (("job",), "job"),  # Prometheus sets job and instance on the scrape target
        (("instance",), "instance"),
        (("job_name", "job_name"), "twice"),
    ],
)
def test_label_names_are_checked(labels, message):
    with pytest.raises(ValueError, match=message):
        Registry().declare(EXIT, "Help.", labels)


@pytest.mark.parametrize(
    ("engine", "prefix"),
    [
        ("pgbackrest", "bdbackup_pgbackrest_"),
        ("mysql-shell", "bdbackup_mysql_shell_"),
        ("my.engine v2", "bdbackup_my_engine_v2_"),
        ("Plug_in9", "bdbackup_Plug_in9_"),
    ],
)
def test_an_engine_name_becomes_a_metric_prefix(engine, prefix):
    assert engine_prefix(engine) == prefix


# ---- rendering ------------------------------------------------------------------------------


def test_a_family_is_rendered_with_help_and_type_once_and_its_series_sorted():
    registry = make_registry()
    samples = [
        sample(1, job_name="pg-b", job_type="pgbackrest"),
        sample(0, job_name="pg-a", job_type="pgbackrest"),
        sample(3, job_name="gfs", job_type="gfs"),
    ]

    text = render(registry, samples)

    assert text == (
        "# HELP bdbackup_job_last_run_exit_code Exit code of the job's last run.\n"
        "# TYPE bdbackup_job_last_run_exit_code gauge\n"
        'bdbackup_job_last_run_exit_code{job_name="gfs",job_type="gfs"} 3\n'
        'bdbackup_job_last_run_exit_code{job_name="pg-a",job_type="pgbackrest"} 0\n'
        'bdbackup_job_last_run_exit_code{job_name="pg-b",job_type="pgbackrest"} 1\n'
    )


def test_families_are_ordered_by_name_and_a_family_without_samples_is_left_out():
    registry = make_registry()
    samples = [
        Sample(SUCCESS, 1),
        Sample(RESULT, 1, {"job_name": "pg", "job_type": "pgbackrest", "result": "exported"}),
        sample(0),
    ]

    text = render(registry, samples)

    names = [line.split()[2] for line in text.splitlines() if line.startswith("# TYPE")]
    assert names == [EXIT, RESULT, SUCCESS]
    assert "bdbackup_unused" not in text
    assert text.count("# HELP") == 3
    assert "bdbackup_metrics_success 1\n" in text  # no labels, no braces


def test_the_order_does_not_depend_on_the_order_of_the_samples():
    registry = make_registry()
    samples = [
        sample(n, job_name=f"job-{n % 4}", job_type=f"type-{n}") for n in range(12)
    ] + [Sample(SUCCESS, 1)]
    expected = render(registry, samples)

    assert expected.count(EXIT + "{") == 12
    for shuffled in (samples[::-1], samples[5:] + samples[:5], samples[1::2] + samples[::2]):
        assert render(registry, shuffled) == expected


def test_labels_are_written_in_the_declared_order():
    registry = make_registry()
    flipped = Sample(EXIT, 1, {"job_type": "gfs", "job_name": "g"})

    assert render(registry, [flipped]).endswith(
        'bdbackup_job_last_run_exit_code{job_name="g",job_type="gfs"} 1\n'
    )


def test_label_values_escape_backslash_quote_and_newline():
    registry = make_registry()

    text = render(registry, [sample(1, job_name='a\\b"c\nd', job_type="x")])

    assert 'job_name="a\\\\b\\"c\\nd"' in text
    assert len(text.splitlines()) == 3  # the newline did not break the line


def test_help_text_escapes_backslash_and_newline():
    registry = Registry()
    registry.declare("bdbackup_x", "Line one\nline two \\ done.")

    text = render(registry, [Sample("bdbackup_x", 1)])

    assert text.splitlines()[:1] == ["# HELP bdbackup_x Line one\\nline two \\\\ done."]


@pytest.mark.parametrize(
    ("value", "written"),
    [
        (0, "0"),
        (5, "5"),
        (5.0, "5"),
        (-3.0, "-3"),
        (2.5, "2.5"),
        (0.1, "0.1"),
        (1e16, "10000000000000000"),
        (1700000000.0, "1700000000"),
        (1700000000.25, "1700000000.25"),
    ],
)
def test_integers_are_written_without_a_decimal_point(value, written):
    text = render(make_registry(), [Sample(SUCCESS, value)])

    assert text.splitlines()[-1:] == [f"{SUCCESS} {written}"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), None, "1", True])
def test_a_value_that_is_not_a_finite_number_is_refused(value):
    with pytest.raises(ValueError, match="value"):
        render(make_registry(), [Sample(SUCCESS, value)])


def test_a_family_that_was_not_declared_is_refused():
    with pytest.raises(ValueError, match="undeclared"):
        render(make_registry(), [Sample("bdbackup_nope", 1)])


@pytest.mark.parametrize(
    "labels",
    [
        {"job_name": "pg"},
        {"job_name": "pg", "job_type": "x", "result": "extra"},
        {"job_name": "pg", "stanza": "main"},
        {},
    ],
    ids=["missing", "extra", "other", "none"],
)
def test_a_sample_with_other_labels_than_declared_is_refused(labels):
    with pytest.raises(ValueError, match="labels"):
        render(make_registry(), [Sample(EXIT, 1, labels)])


def test_two_samples_of_one_series_are_refused():
    # node_exporter rejects a file that repeats a series, so none is ever written.
    with pytest.raises(ValueError, match="duplicate"):
        render(make_registry(), [sample(0), sample(1)])


def test_no_samples_render_nothing_where_one_sample_renders_its_family():
    assert render(make_registry(), []) == ""
    assert render(make_registry(), [Sample(SUCCESS, 1)]) != ""
