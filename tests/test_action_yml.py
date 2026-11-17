import json
import os
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ACTION = Path(__file__).resolve().parents[1] / "action.yml"


def step_script(name):
    steps = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == name)


def run_block(script, tmp_path, **env):
    out = tmp_path / "gh_output"
    out.write_text("")
    full_env = {**os.environ, "GITHUB_OUTPUT": str(out), "RUNNER_TEMP": str(tmp_path), **env}
    proc = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                          env=full_env, capture_output=True, text=True)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return proc, outputs


def test_guard_skips_self_healing_branches_to_avoid_loops(tmp_path):
    script = step_script("Resolve branch and loop guard")

    _, healed = run_block(script, tmp_path, INPUT_BRANCH="self-healing/feature/x", REF_NAME="main")
    _, normal = run_block(script, tmp_path, INPUT_BRANCH="", REF_NAME="feature/x")

    assert healed == {"branch": "self-healing/feature/x", "skip": "true"}
    assert normal == {"branch": "feature/x", "skip": "false"}


def test_heal_step_passes_inputs_and_exposes_status(tmp_path):
    action_dir = tmp_path / "action"
    action_dir.mkdir()
    (action_dir / "heal.py").write_text(
        "import json, sys\n"
        "a = sys.argv\n"
        "json.dump({'status': 'success', 'args': a[1:]}, open(a[a.index('--result-file') + 1], 'w'))\n"
        "sys.exit(0)\n"
    )
    proc, outputs = run_block(
        step_script("Diagnose and fix"), tmp_path,
        GITHUB_ACTION_PATH=str(action_dir), GITHUB_WORKSPACE="/ws",
        TEST_CMD="pytest -q", SETUP_CMD="pip install -r requirements.txt", IMAGE="python:3.11",
        MAX_ATTEMPTS="2", MODEL="m", REPOSITORY="o/r",
    )

    assert proc.returncode == 0, proc.stderr
    assert outputs["status"] == "success" and outputs["exit_code"] == "0"
    args = json.loads(Path(outputs["result_file"]).read_text())["args"]
    assert args[:1] == ["/ws"]
    for expected in (["--test-cmd", "pytest -q"], ["--setup-cmd", "pip install -r requirements.txt"],
                     ["--image", "python:3.11"], ["--max-attempts", "2"], ["--trace-tag", "gha:o/r"]):
        assert args[args.index(expected[0]) + 1] == expected[1]


def test_optional_flags_are_left_out_when_empty_and_a_crash_does_not_abort_the_step(tmp_path):
    action_dir = tmp_path / "action"
    action_dir.mkdir()
    (action_dir / "heal.py").write_text("import sys\nsys.exit(1)\n")  # crashes, writes no result file

    proc, outputs = run_block(
        step_script("Diagnose and fix"), tmp_path,
        GITHUB_ACTION_PATH=str(action_dir), GITHUB_WORKSPACE="/ws",
        TEST_CMD="pytest", SETUP_CMD="", IMAGE="", MAX_ATTEMPTS="3", MODEL="m", REPOSITORY="o/r",
    )

    assert proc.returncode == 0, proc.stderr          # the step itself survives, so later steps can report
    assert outputs["status"] == "unknown" and outputs["exit_code"] == "1"


def test_platform_flag_is_passed_through_when_set(tmp_path):
    action_dir = tmp_path / "action"
    action_dir.mkdir()
    (action_dir / "heal.py").write_text(
        "import json, sys\n"
        "a = sys.argv\n"
        "json.dump({'status': 'success', 'args': a[1:]}, open(a[a.index('--result-file') + 1], 'w'))\n"
        "sys.exit(0)\n"
    )

    proc, outputs = run_block(
        step_script("Diagnose and fix"), tmp_path,
        GITHUB_ACTION_PATH=str(action_dir), GITHUB_WORKSPACE="/ws",
        TEST_CMD="pytest -q", SETUP_CMD="pip install -r requirements.txt", IMAGE="python:3.11",
        PLATFORM="linux/amd64", MAX_ATTEMPTS="2", MODEL="m", REPOSITORY="o/r",
    )

    assert proc.returncode == 0, proc.stderr
    args = json.loads(Path(outputs["result_file"]).read_text())["args"]
    assert "--platform" in args
    assert args[args.index("--platform") + 1] == "linux/amd64"


# --- Docker image cache -------------------------------------------------------------------------

import shutil

CACHE_SCRIPT = ACTION.parent / "scripts" / "image_cache.sh"


def step_names():
    return [s.get("name") for s in yaml.safe_load(ACTION.read_text())["runs"]["steps"]]


def cache_key(tmp_path, image="python:3.11", setup="pip install pytest", requirements=None):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    if requirements is not None:
        (work / "requirements.txt").write_text(requirements)
    env = {**os.environ, "HEAL_IMAGE": image, "HEAL_SETUP_CMD": setup, "WORKSPACE": str(work)}
    return subprocess.run(["bash", str(CACHE_SCRIPT), "key"], env=env, capture_output=True, text=True, check=True).stdout.strip()


def test_cache_steps_run_in_the_right_order():
    names = step_names()

    assert names.index("Restore Docker image cache") < names.index("Load cached Docker images") < names.index("Diagnose and fix")
    assert names.index("Diagnose and fix") < names.index("Collect Docker images for the cache") < names.index("Save Docker image cache")
    assert names.index("Save Docker image cache") < names.index("Fail if no fix was found")  # saved even when the job ends red


def test_cache_key_is_stable_and_changes_with_what_the_images_contain(tmp_path):
    base = cache_key(tmp_path, requirements="pytest==9\n")

    assert cache_key(tmp_path, requirements="pytest==9\n") == base
    assert cache_key(tmp_path, requirements="pytest==8\n") != base
    assert cache_key(tmp_path, setup="pip install pytest ruff", requirements="pytest==9\n") != base
    assert cache_key(tmp_path, image="python:3.12", requirements="pytest==9\n") != base


def test_collect_step_writes_only_key_value_lines_to_github_output(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text('#!/usr/bin/env bash\n[ "$1" = images ] && echo "self-heal-setup:abc123"\n[ "$1" = save ] && echo "image-bytes"\nexit 0\n')
    docker.chmod(0o755)

    proc, outputs = run_block(
        step_script("Collect Docker images for the cache"), tmp_path,
        GITHUB_ACTION_PATH=str(ACTION.parent), PATH=f"{fake_bin}:{os.environ['PATH']}",
    )

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "gh_output").read_text().strip() == "saved=true"   # the 'Cached 1 image(s)' message is not in it
    assert (tmp_path / "heal-images" / "images.tar.gz").is_file()


def test_nothing_to_cache_reports_saved_false(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n")      # `docker images` prints nothing
    docker.chmod(0o755)

    proc, outputs = run_block(
        step_script("Collect Docker images for the cache"), tmp_path,
        GITHUB_ACTION_PATH=str(ACTION.parent), PATH=f"{fake_bin}:{os.environ['PATH']}",
    )

    assert proc.returncode == 0 and outputs == {"saved": "false"}


@pytest.mark.skipif(shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
                    reason="needs a running Docker daemon")
def test_images_survive_a_real_save_and_load_round_trip(tmp_path):
    tag = "self-heal-setup:cachetest0001"
    (tmp_path / "Dockerfile").write_text("FROM scratch\nLABEL cache-test=1\n")
    subprocess.run(["docker", "build", "-q", "-t", tag, str(tmp_path)], check=True, capture_output=True)
    archive = tmp_path / "out" / "images.tar.gz"
    try:
        only_this_image = {**os.environ, "HEAL_IMAGE_FILTER": f"^{tag}$"}   # not every image on the machine
        saved = subprocess.run(["bash", str(CACHE_SCRIPT), "save", str(archive)], env=only_this_image, capture_output=True, text=True, check=True)
        assert "saved=true" in saved.stdout and archive.is_file()
        subprocess.run(["docker", "rmi", "-f", tag], check=True, capture_output=True)

        subprocess.run(["bash", str(CACHE_SCRIPT), "load", str(archive)], check=True, capture_output=True)

        assert subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
