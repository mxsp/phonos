import importlib.util
import os
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / 'node1-esm.py'
ROOT_PATH = MODULE_PATH.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))
os.environ.setdefault('ENCRYPTION_KEY', 'A' * 44)
SPEC = importlib.util.spec_from_file_location('node1_esm', MODULE_PATH)
node1_esm = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(node1_esm)

FoldingJob = node1_esm.FoldingJob
ExternalRunnerConfig = node1_esm.ExternalRunnerConfig
parse_job_payload = node1_esm.parse_job_payload
build_external_command = node1_esm.build_external_command
run_external_esmfold = node1_esm.run_external_esmfold


def test_parse_job_payload_requires_sequence(tmp_path):
    job = {
        'job_id': 'job-1',
        'task': 'protein_folding',
        'params': {},
        'output_path': str(tmp_path / 'result.pdb'),
    }

    normalized, error = parse_job_payload(job)

    assert normalized is None
    assert error == 'Missing sequence in job parameters.'


def test_build_external_command_uses_argument_list(tmp_path):
    job_request = FoldingJob(
        job_id='job-42',
        sequence='ACDEFGHIK',
        output_path=(tmp_path / 'job-42.pdb'),
        output_dir=tmp_path,
    )
    config = ExternalRunnerConfig(
        script_path=tmp_path / 'launch.sh',
        model_name='esmfold_v1',
        timeout=60,
    )
    fasta_path = tmp_path / 'input.fasta'

    command = build_external_command(job_request, config, fasta_path)

    assert isinstance(command, list)
    assert command[0] == str(config.script_path)
    assert '--sequence' in command and command[command.index('--sequence') + 1] == str(fasta_path)
    assert '--output_name' in command and command[command.index('--output_name') + 1] == 'job-42.pdb'


def test_run_external_esmfold_handles_missing_script(tmp_path):
    job_request = FoldingJob(
        job_id='job-7',
        sequence='ACDEFGHIKLM',
        output_path=(tmp_path / 'job-7.pdb'),
        output_dir=tmp_path,
    )
    config = ExternalRunnerConfig(
        script_path=tmp_path / 'missing-launch.sh',
        model_name='esmfold_v1',
        timeout=30,
    )

    result = run_external_esmfold(job_request, config)

    assert result['status'] == 'error'
    assert 'not found' in (result.get('message') or '').lower()
    assert 'execution_time' in result
