# pylint doesn't know about pytest fixtures
# pylint: disable=unused-argument

import os
import shutil
from contextlib import contextmanager

import pytest
from dagster_celery import celery_executor

from dagster import (
    CompositeSolidExecutionResult,
    InputDefinition,
    Int,
    ModeDefinition,
    Output,
    OutputDefinition,
    PipelineExecutionResult,
    RetryRequested,
    SolidExecutionResult,
    default_executors,
    execute_pipeline,
    lambda_solid,
    pipeline,
    seven,
    solid,
)
from dagster.core.definitions.reconstructable import ReconstructablePipeline
from dagster.core.errors import DagsterSubprocessError
from dagster.core.instance import DagsterInstance
from dagster.core.test_utils import nesting_composite_pipeline

celery_mode_defs = [ModeDefinition(executor_defs=default_executors + [celery_executor])]

BUILDKITE = os.getenv('BUILDKITE')
skip_ci = pytest.mark.skipif(
    bool(BUILDKITE),
    reason='Tests hang forever on buildkite for reasons we don\'t currently understand',
)

COMPOSITE_DEPTH = 3


@solid
def simple(_):
    return 1


@solid
def add_one(_, num):
    return num + 1


@pipeline(mode_defs=celery_mode_defs)
def test_pipeline():
    return simple()


@pipeline(mode_defs=celery_mode_defs)
def test_serial_pipeline():
    return add_one(simple())


@solid(output_defs=[OutputDefinition(name='value_one'), OutputDefinition(name='value_two')])
def emit_values(_context):
    yield Output(1, 'value_one')
    yield Output(2, 'value_two')


@lambda_solid(input_defs=[InputDefinition('num_one'), InputDefinition('num_two')])
def subtract(num_one, num_two):
    return num_one - num_two


@pipeline(mode_defs=celery_mode_defs)
def test_diamond_pipeline():
    value_one, value_two = emit_values()
    return subtract(num_one=add_one(num=value_one), num_two=add_one.alias('renamed')(num=value_two))


@pipeline(mode_defs=celery_mode_defs)
def test_parallel_pipeline():
    value = simple()
    for i in range(10):
        add_one.alias('add_one_' + str(i))(value)


@pipeline(mode_defs=celery_mode_defs)
def test_more_parallel_pipeline():
    value = simple()
    for i in range(500):
        add_one.alias('add_one_' + str(i))(value)


def composite_pipeline():
    return nesting_composite_pipeline(COMPOSITE_DEPTH, 2, mode_defs=celery_mode_defs)


@solid(
    output_defs=[
        OutputDefinition(Int, 'out_1', is_required=False),
        OutputDefinition(Int, 'out_2', is_required=False),
        OutputDefinition(Int, 'out_3', is_required=False),
    ]
)
def foo(_):
    yield Output(1, 'out_1')


@solid
def bar(_, input_arg):
    return input_arg


@pipeline(mode_defs=celery_mode_defs)
def test_optional_outputs():
    # pylint: disable=no-member
    foo_res = foo()
    bar.alias('first_consumer')(input_arg=foo_res.out_1)
    bar.alias('second_consumer')(input_arg=foo_res.out_2)
    bar.alias('third_consumer')(input_arg=foo_res.out_3)


@lambda_solid
def fails():
    raise Exception('argjhgjh')


@lambda_solid
def should_never_execute(_):
    assert False  # should never execute


@pipeline(mode_defs=celery_mode_defs)
def test_fails():
    should_never_execute(fails())


@lambda_solid
def retry_request():
    raise RetryRequested()


@pipeline(mode_defs=celery_mode_defs)
def test_retries():
    retry_request()


def events_of_type(result, event_type):
    return [event for event in result.event_list if event.event_type_value == event_type]


@solid(config_schema=str)
def destroy(context, x):
    shutil.rmtree(context.solid_config)
    return x


@pipeline(mode_defs=celery_mode_defs)
def engine_error():
    a = simple()
    b = destroy(a)

    subtract(a, b)


@solid(
    tags={
        'dagster-k8s/resource_requirements': {
            'requests': {'cpu': '250m', 'memory': '64Mi'},
            'limits': {'cpu': '500m', 'memory': '2560Mi'},
        }
    }
)
def resource_req_solid(context):
    context.log.info('running')


@pipeline(mode_defs=celery_mode_defs)
def test_resources_limit():
    resource_req_solid()


@contextmanager
def execute_pipeline_on_celery(pipeline_name,):
    with seven.TemporaryDirectory() as tempdir:
        result = execute_pipeline(
            ReconstructablePipeline.for_file(__file__, pipeline_name),
            run_config={
                'storage': {'filesystem': {'config': {'base_dir': tempdir}}},
                'execution': {'celery': {}},
            },
            instance=DagsterInstance.local_temp(tempdir=tempdir),
        )
        yield result


@contextmanager
def execute_eagerly_on_celery(pipeline_name, instance=None, subset=None):
    with seven.TemporaryDirectory() as tempdir:
        instance = instance or DagsterInstance.local_temp(tempdir=tempdir)
        result = execute_pipeline(
            ReconstructablePipeline.for_file(__file__, pipeline_name).subset_for_execution(subset),
            run_config={
                'storage': {'filesystem': {'config': {'base_dir': tempdir}}},
                'execution': {'celery': {'config': {'config_source': {'task_always_eager': True}}}},
            },
            instance=instance,
        )
        yield result


@skip_ci
def test_execute_on_celery(dagster_celery_worker):
    with execute_pipeline_on_celery('test_pipeline') as result:
        assert result.result_for_solid('simple').output_value() == 1
        assert len(result.step_event_list) == 4
        assert len(events_of_type(result, 'STEP_START')) == 1
        assert len(events_of_type(result, 'STEP_OUTPUT')) == 1
        assert len(events_of_type(result, 'OBJECT_STORE_OPERATION')) == 1
        assert len(events_of_type(result, 'STEP_SUCCESS')) == 1


@skip_ci
def test_execute_serial_on_celery(dagster_celery_worker):
    with execute_pipeline_on_celery('test_serial_pipeline') as result:
        assert result.result_for_solid('simple').output_value() == 1
        assert result.result_for_solid('add_one').output_value() == 2
        assert len(result.step_event_list) == 10
        assert len(events_of_type(result, 'STEP_START')) == 2
        assert len(events_of_type(result, 'STEP_INPUT')) == 1
        assert len(events_of_type(result, 'STEP_OUTPUT')) == 2
        assert len(events_of_type(result, 'OBJECT_STORE_OPERATION')) == 3
        assert len(events_of_type(result, 'STEP_SUCCESS')) == 2


@skip_ci
def test_execute_diamond_pipeline_on_celery(dagster_celery_worker):
    with execute_pipeline_on_celery('test_diamond_pipeline') as result:
        assert result.result_for_solid('emit_values').output_values == {
            'value_one': 1,
            'value_two': 2,
        }
        assert result.result_for_solid('add_one').output_value() == 2
        assert result.result_for_solid('renamed').output_value() == 3
        assert result.result_for_solid('subtract').output_value() == -1


@skip_ci
def test_execute_parallel_pipeline_on_celery(dagster_celery_worker):
    with execute_pipeline_on_celery('test_parallel_pipeline') as result:
        assert len(result.solid_result_list) == 11


@skip_ci
@pytest.mark.skip
def test_execute_more_parallel_pipeline_on_celery():
    with execute_pipeline_on_celery('test_more_parallel_pipeline') as result:
        assert len(result.solid_result_list) == 501


@skip_ci
def test_execute_composite_pipeline_on_celery(dagster_celery_worker):
    with execute_pipeline_on_celery('composite_pipeline') as result:
        assert result.success
        assert isinstance(result, PipelineExecutionResult)
        assert len(result.solid_result_list) == 1
        composite_solid_result = result.solid_result_list[0]
        assert len(composite_solid_result.solid_result_list) == 2
        for r in composite_solid_result.solid_result_list:
            assert isinstance(r, CompositeSolidExecutionResult)
        composite_solid_results = composite_solid_result.solid_result_list
        for i in range(COMPOSITE_DEPTH):
            next_level = []
            assert len(composite_solid_results) == pow(2, i + 1)
            for res in composite_solid_results:
                assert isinstance(res, CompositeSolidExecutionResult)
                for r in res.solid_result_list:
                    next_level.append(r)
            composite_solid_results = next_level
        assert len(composite_solid_results) == pow(2, COMPOSITE_DEPTH + 1)
        assert all(
            (isinstance(r, SolidExecutionResult) and r.success for r in composite_solid_results)
        )


@skip_ci
def test_execute_optional_outputs_pipeline_on_celery(dagster_celery_worker):
    with execute_pipeline_on_celery('test_optional_outputs') as result:
        assert len(result.solid_result_list) == 4
        assert sum([int(x.skipped) for x in result.solid_result_list]) == 2
        assert sum([int(x.success) for x in result.solid_result_list]) == 2


@skip_ci
def test_execute_fails_pipeline_on_celery(dagster_celery_worker):
    with execute_pipeline_on_celery('test_fails') as result:
        assert len(result.solid_result_list) == 2  # fail & skip
        assert not result.result_for_solid('fails').success
        assert (
            result.result_for_solid('fails').failure_data.error.message == 'Exception: argjhgjh\n'
        )
        assert result.result_for_solid('should_never_execute').skipped


def test_execute_eagerly_on_celery():
    with seven.TemporaryDirectory() as tempdir:
        instance = DagsterInstance.local_temp(tempdir=tempdir)
        with execute_eagerly_on_celery('test_pipeline', instance) as result:
            assert result.result_for_solid('simple').output_value() == 1
            assert len(result.step_event_list) == 4
            assert len(events_of_type(result, 'STEP_START')) == 1
            assert len(events_of_type(result, 'STEP_OUTPUT')) == 1
            assert len(events_of_type(result, 'OBJECT_STORE_OPERATION')) == 1
            assert len(events_of_type(result, 'STEP_SUCCESS')) == 1

            events = instance.all_logs(result.run_id)
            start_markers = {}
            end_markers = {}
            for event in events:
                dagster_event = event.dagster_event
                if dagster_event.is_engine_event:
                    if dagster_event.engine_event_data.marker_start:
                        key = '{step}.{marker}'.format(
                            step=event.step_key, marker=dagster_event.engine_event_data.marker_start
                        )
                        start_markers[key] = event.timestamp
                    if dagster_event.engine_event_data.marker_end:
                        key = '{step}.{marker}'.format(
                            step=event.step_key, marker=dagster_event.engine_event_data.marker_end
                        )
                        end_markers[key] = event.timestamp

            seen = set()
            assert set(start_markers.keys()) == set(end_markers.keys())
            for key in end_markers:
                assert end_markers[key] - start_markers[key] > 0
                seen.add(key)


def test_execute_eagerly_serial_on_celery():
    with execute_eagerly_on_celery('test_serial_pipeline') as result:
        assert result.result_for_solid('simple').output_value() == 1
        assert result.result_for_solid('add_one').output_value() == 2
        assert len(result.step_event_list) == 10
        assert len(events_of_type(result, 'STEP_START')) == 2
        assert len(events_of_type(result, 'STEP_INPUT')) == 1
        assert len(events_of_type(result, 'STEP_OUTPUT')) == 2
        assert len(events_of_type(result, 'OBJECT_STORE_OPERATION')) == 3
        assert len(events_of_type(result, 'STEP_SUCCESS')) == 2


def test_execute_eagerly_diamond_pipeline_on_celery():
    with execute_eagerly_on_celery('test_diamond_pipeline') as result:
        assert result.result_for_solid('emit_values').output_values == {
            'value_one': 1,
            'value_two': 2,
        }
        assert result.result_for_solid('add_one').output_value() == 2
        assert result.result_for_solid('renamed').output_value() == 3
        assert result.result_for_solid('subtract').output_value() == -1


def test_execute_eagerly_diamond_pipeline_subset_on_celery():
    with execute_eagerly_on_celery('test_diamond_pipeline', subset=['emit_values']) as result:
        assert result.result_for_solid('emit_values').output_values == {
            'value_one': 1,
            'value_two': 2,
        }
        assert len(result.solid_result_list) == 1


def test_execute_eagerly_parallel_pipeline_on_celery():
    with execute_eagerly_on_celery('test_parallel_pipeline') as result:
        assert len(result.solid_result_list) == 11


@pytest.mark.skip
def test_execute_eagerly_more_parallel_pipeline_on_celery():
    with execute_eagerly_on_celery('test_more_parallel_pipeline') as result:
        assert len(result.solid_result_list) == 501


def test_execute_eagerly_composite_pipeline_on_celery():
    with execute_eagerly_on_celery('composite_pipeline') as result:
        assert result.success
        assert isinstance(result, PipelineExecutionResult)
        assert len(result.solid_result_list) == 1
        composite_solid_result = result.solid_result_list[0]
        assert len(composite_solid_result.solid_result_list) == 2
        for r in composite_solid_result.solid_result_list:
            assert isinstance(r, CompositeSolidExecutionResult)
        composite_solid_results = composite_solid_result.solid_result_list
        for i in range(COMPOSITE_DEPTH):
            next_level = []
            assert len(composite_solid_results) == pow(2, i + 1)
            for res in composite_solid_results:
                assert isinstance(res, CompositeSolidExecutionResult)
                for r in res.solid_result_list:
                    next_level.append(r)
            composite_solid_results = next_level
        assert len(composite_solid_results) == pow(2, COMPOSITE_DEPTH + 1)
        assert all(
            (isinstance(r, SolidExecutionResult) and r.success for r in composite_solid_results)
        )


def test_execute_eagerly_optional_outputs_pipeline_on_celery():
    with execute_eagerly_on_celery('test_optional_outputs') as result:
        assert len(result.solid_result_list) == 4
        assert sum([int(x.skipped) for x in result.solid_result_list]) == 2
        assert sum([int(x.success) for x in result.solid_result_list]) == 2


def test_execute_eagerly_resources_limit_pipeline_on_celery():
    with execute_eagerly_on_celery('test_resources_limit') as result:
        assert result.result_for_solid('resource_req_solid').success
        assert result.success


def test_execute_eagerly_fails_pipeline_on_celery():
    with execute_eagerly_on_celery('test_fails') as result:
        assert len(result.solid_result_list) == 2
        assert not result.result_for_solid('fails').success
        assert (
            result.result_for_solid('fails').failure_data.error.message == 'Exception: argjhgjh\n'
        )
        assert result.result_for_solid('should_never_execute').skipped


def test_execute_eagerly_retries_pipeline_on_celery():
    with execute_eagerly_on_celery('test_retries') as result:
        assert len(events_of_type(result, 'STEP_START')) == 1
        assert len(events_of_type(result, 'STEP_UP_FOR_RETRY')) == 1
        assert len(events_of_type(result, 'STEP_RESTARTED')) == 1
        assert len(events_of_type(result, 'STEP_FAILURE')) == 1


@pytest.mark.skip('https://github.com/dagster-io/dagster/issues/2439')
def test_bad_broker():
    pass
    # with pytest.raises(check.CheckError) as exc_info:
    #     event_stream = execute_pipeline_iterator(
    #         ExecutionTargetHandle.for_pipeline_python_file(
    #             __file__, 'test_diamond_pipeline'
    #         ).build_pipeline_definition(),
    #         run_config={
    #             'storage': {'filesystem': {}},
    #             'execution': {'celery': {'config': {'broker': 'notlocal.bad'}}},
    #         },
    #         instance=DagsterInstance.local_temp(),
    #     )
    #     list(event_stream)
    # assert 'Must use S3 or GCS storage with non-local Celery' in str(exc_info.value)


def test_engine_error():
    with pytest.raises(DagsterSubprocessError):
        with seven.TemporaryDirectory() as tempdir:
            storage = os.path.join(tempdir, 'flakey_storage')
            execute_pipeline(
                ReconstructablePipeline.for_file(__file__, 'engine_error'),
                run_config={
                    'storage': {'filesystem': {'config': {'base_dir': storage}}},
                    'execution': {
                        'celery': {'config': {'config_source': {'task_always_eager': True}}}
                    },
                    'solids': {'destroy': {'config': storage}},
                },
                instance=DagsterInstance.local_temp(tempdir=tempdir),
            )
