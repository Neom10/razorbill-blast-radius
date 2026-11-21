# Razorbill v2.1 · RWX Blast Radius

This update is applied to an existing v2 checkout. Upload the four extracted files to the repository root together and keep the v2 profile.json.gz and coverage_map.py. The official sample scored 127.493 simulated seconds with zero missed commits. The hidden-practice score for this update is unknown.

The selector broadens predictions when imports change and when a test setup or teardown hook affects a whole class. It also schedules additional cheap tests on existing workers, using idle time and a maximum of 350 extra milliseconds of test time for each commit with a nonempty schedule. A fresh class-setup regression caused 50 test errors in the full affected module. This update included all 50. V2 missed 47. The three previously created independent patches also retained all failing tests.

## How the selector works

The map was built by running pristine SQLGlot at commit eb11aec4be8cb2122afe713a4816662c3f0fb2fa, with integration tests enabled. All 1,620 numbered tests in the sample catalog have passing profile records. The generator runs three disjoint test groups and combines their observations. The map contains per-test line, anonymous-function, fixture-read, SQL expression class, dialect and comparison-operand type coverage, along with baseline Python source used to interpret incoming diffs. Tests are identified by full pytest ID. The map generator consumes the pinned checkout. It has no sample patches, failure labels or hidden-commit inputs.

The profiling harness traces process-pool work within its originating test. It serializes and deserializes arguments and results and runs the worker function in the tracing process. This captures optimizer and SQL expression serialization dependencies. A real spawned process can behave differently. Map generation excludes allocations traced during tracemalloc measurements. Release validation used ordinary process pools for the three additional patches.

At prediction time the selector applies the diff to baseline source in memory and compares Python syntax trees. For a localized expression edit it consults the tests that reached the enclosing statement. It tracks class-level dispatch lambdas independently from module initialization. For identity/equality edits it examines the operand types observed during the original run and can drop a test where the types establish equivalent comparisons. It treats user-defined equality conservatively. Shared settings, test fixtures and unrecognized changes use broader selection. Up to eight worker counts are compared using recorded durations and the 2,000 ms worker charge.

Coverage can miss dependencies introduced by a new path, cached fixtures or reflection. The official sample informed development. The local score cannot establish a practice or final rank.

## Reproduce a score

With the official kit extracted in a sibling directory named starter:

```sh
python check.py
python selector.py ../starter/sample > predictions.jsonl
python score.py ../starter/sample ../starter/sample/answers.json predictions.jsonl
```

With Docker and Make, run the official container harness:

```sh
make sample SAMPLE="$(cd ../starter/sample && pwd)"
```

The active Dockerfile uses the organizer's pinned Python image. The entry point was tested on Python 3.13 using read-only application and input directories. The hosted Docker build and hidden-practice result are pending.

## Regenerate the baseline profile

The shipped profile is ready to use. Profile.Dockerfile and its Python scripts are provided to explain and reproduce it from the pinned repository. Building the profile needs network access and more time than the submission image. Export it into a separate directory:

```sh
docker buildx build --platform linux/amd64 -f Profile.Dockerfile --output type=local,dest=regenerated .
```

The shipped profile was collected on Linux using Python 3.13.5 with test dependencies pinned from the kit. Host versions, collection order and caches can change traces slightly. The SQLGlot source license is included as SQLGlot-LICENSE.txt.
