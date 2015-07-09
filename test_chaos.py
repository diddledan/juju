import os
import stat
from tempfile import NamedTemporaryFile
from unittest import TestCase

from mock import patch
import yaml

from jujupy import (
    EnvJujuClient,
    SimpleEnvironment,
    )
from chaos import (
    background_chaos,
    MonkeyRunner,
    )
from test_jujupy import (
    assert_juju_call,
    )


def fake_EnvJujuClient_by_version(env, path=None, debug=None):
    return EnvJujuClient(env=env, version='1.2.3.4', full_path=path)


def fake_SimpleEnvironment_from_config(name):
    return SimpleEnvironment(name, {})


class TestBackgroundChaos(TestCase):

    def test_background_chaos(self):
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo/juju')
        with patch('chaos.MonkeyRunner.deploy_chaos_monkey',
                   autospec=True) as d_mock:
            with patch('chaos.MonkeyRunner.unleash_once',
                       autospec=True) as u_mock:
                with patch('chaos.MonkeyRunner.wait_for_chaos',
                           autospec=True) as w_mock:
                    with background_chaos('foo', client):
                        pass
        self.assertEqual(1, d_mock.call_count)
        self.assertEqual(1, u_mock.call_count)
        self.assertEqual({'state': 'start'}, w_mock.mock_calls[0][2])
        self.assertEqual({'state': 'complete'}, w_mock.mock_calls[1][2])

    def test_background_chaos_exits(self):
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo/juju')
        with patch('chaos.MonkeyRunner.deploy_chaos_monkey',
                   autospec=True):
            with patch('chaos.MonkeyRunner.unleash_once',
                       autospec=True):
                with patch('chaos.MonkeyRunner.wait_for_chaos',
                           autospec=True):
                    with patch('logging.exception') as le_mock:
                        with patch('sys.exit', autospec=True) as se_mock:
                            with background_chaos('foo', client):
                                raise Exception()
        self.assertEqual(1, le_mock.call_count)
        se_mock.assert_called_once_with(1)


class TestRunChaosMonkey(TestCase):

    def test_deploy_chaos_monkey(self):
        def output(args, **kwargs):
            status = yaml.safe_dump({
                'machines': {
                    '0': {'agent-state': 'started'}
                },
                'services': {
                    'ser1': {
                        'units': {
                            'bar': {
                                'agent-state': 'started',
                                'subordinates': {
                                    'chaos-monkey/1': {
                                        'agent-state': 'started'
                                    }
                                }
                            }
                        }
                    }
                }
            })
            output = {
                ('juju', '--show-log', 'status', '-e', 'foo'): status,
                }
            return output[args]
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo/juju')
        with patch('subprocess.check_output', side_effect=output,
                   autospec=True) as co_mock:
            with patch('subprocess.check_call', autospec=True) as cc_mock:
                monkey_runner = MonkeyRunner('foo', client, service='ser1')
                with patch('sys.stdout', autospec=True):
                    monkey_runner.deploy_chaos_monkey()
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'deploy', '-e', 'foo', 'local:chaos-monkey'),
            0)
        assert_juju_call(self, cc_mock, client, (
            'juju', '--show-log', 'add-relation', '-e', 'foo', 'ser1',
            'chaos-monkey'), 1)
        self.assertEqual(cc_mock.call_count, 2)
        self.assertEqual(co_mock.call_count, 2)

    def test_iter_chaos_monkey_units(self):
        def output(args, **kwargs):
            status = yaml.safe_dump({
                'machines': {
                    '0': {'agent-state': 'started'}
                },
                'services': {
                    'jenkins': {
                        'units': {
                            'foo': {
                                'subordinates': {
                                    'chaos-monkey/0': {'baz': 'qux'},
                                    'not-chaos/0': {'qwe': 'rty'},
                                }
                            },
                            'bar': {
                                'subordinates': {
                                    'chaos-monkey/1': {'abc': '123'},
                                }
                            }
                        }
                    }
                }
            })
            output = {
                ('juju', '--show-log', 'status', '-e', 'foo'): status,
                }
            return output[args]
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo/juju')
        runner = MonkeyRunner('foo', client, service='jenkins')
        with patch('subprocess.check_output', side_effect=output,
                   autospec=True):
            monkey_units = dict((k, v) for k, v in
                                runner.iter_chaos_monkey_units())
        expected = {
            'chaos-monkey/0': {'baz': 'qux'},
            'chaos-monkey/1': {'abc': '123'}
        }
        self.assertEqual(expected, monkey_units)

    def test_get_unit_status(self):
        def output(args, **kwargs):
            status = yaml.safe_dump({
                'machines': {
                    '0': {'agent-state': 'started'}
                },
                'services': {
                    'jenkins': {
                        'units': {
                            'foo': {
                                'subordinates': {
                                    'chaos-monkey/0': {'baz': 'qux'},
                                    'not-chaos/0': {'qwe': 'rty'},
                                }
                            },
                            'bar': {
                                'subordinates': {
                                    'chaos-monkey/1': {'abc': '123'},
                                }
                            }
                        }
                    }
                }
            })
            charm_config = yaml.safe_dump({
                'charm': {'jenkins'},
                'service': {'jenkins'},
                'settings': {
                    'chaos-dir': {
                        'default': 'true',
                        'description': 'bla bla',
                        'type': 'string',
                        'value': '/tmp/charm-dir',
                    }
                }
            })
            output = {
                ('juju', '--show-log', 'status', '-e', 'foo'): status,
                ('juju', '--show-log', 'get', '-e', 'foo', 'chaos-monkey'
                 ): charm_config,
                }
            return output[args]
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo')
        monkey_runner = MonkeyRunner('foo', client, service='jenkins')
        monkey_runner.monkey_ids = {
            'chaos-monkey/0': 'workspace0',
            'chaos-monkey/1': 'workspace1'
        }
        with patch('subprocess.check_output', side_effect=output,
                   autospec=True):
            with patch('subprocess.call', autospec=True,
                       return_value=0) as call_mock:
                for unit_name in ['chaos-monkey/1', 'chaos-monkey/0']:
                    with patch('sys.stdout', autospec=True):
                        self.assertEqual(
                            monkey_runner.get_unit_status(unit_name),
                            'running')
            self.assertEqual(call_mock.call_count, 2)
        with patch('subprocess.check_output', side_effect=output,
                   autospec=True):
            with patch('subprocess.call', autospec=True,
                       return_value=1) as call_mock:
                for unit_name in ['chaos-monkey/1', 'chaos-monkey/0']:
                    with patch('sys.stdout', autospec=True):
                        self.assertEqual(
                            monkey_runner.get_unit_status(unit_name),
                            'done')
            self.assertEqual(call_mock.call_count, 2)


class TestUnleashOnce(TestCase):

    def test_unleash_once(self):
        def output(args, **kwargs):
            status = yaml.safe_dump({
                'machines': {
                    '0': {'agent-state': 'started'}
                },
                'services': {
                    'jenkins': {
                        'units': {
                            'foo': {
                                'subordinates': {
                                    'chaos-monkey/0': {'baz': 'qux'},
                                    'not-chaos/0': {'qwe': 'rty'},
                                }
                            },
                            'bar': {
                                'subordinates': {
                                    'chaos-monkey/1': {'abc': '123'},
                                }
                            }
                        }
                    }
                }
            })
            charm_config = yaml.safe_dump({
                'charm': {'jenkins'},
                'service': {'jenkins'},
                'settings': {
                    'chaos-dir': {
                        'default': 'true',
                        'description': 'bla bla',
                        'type': 'string',
                        'value': '/tmp/charm-dir',
                    }
                }
            })
            output = {
                ('juju', '--show-log', 'status', '-e', 'foo'): status,
                ('juju', '--show-log', 'get', '-e', 'foo', 'jenkins'
                 ): charm_config,
                ('juju', '--show-log', 'action', 'do', '-e', 'foo',
                 'chaos-monkey/0', 'start', 'mode=single',
                 'enablement-timeout=0'
                 ): 'Action queued with id: abcd',
                ('juju', '--show-log', 'action', 'do', '-e', 'foo',
                 'chaos-monkey/0', 'start', 'mode=single',
                 'enablement-timeout=0', 'monkey-id=abcd'
                 ): 'Action queued with id: efgh',
                ('juju', '--show-log', 'action', 'do', '-e', 'foo',
                 'chaos-monkey/1', 'start', 'mode=single',
                 'enablement-timeout=0'
                 ): 'Action queued with id: 1234',
                ('juju', '--show-log', 'action', 'do', '-e', 'foo',
                 'chaos-monkey/1', 'start', 'mode=single',
                 'enablement-timeout=0', 'monkey-id=1234'
                 ): 'Action queued with id: 5678',
                }
            return output[args]

        def output2(args, **kwargs):
            status = yaml.safe_dump({
                'machines': {
                    '0': {'agent-state': 'started'}
                },
                'services': {
                    'jenkins': {
                        'units': {
                            'bar': {
                                'subordinates': {
                                    'chaos-monkey/1': {'abc': '123'},
                                }
                            }
                        }
                    }
                }
            })
            output = {
                ('juju', '--show-log', 'status', '-e', 'foo'): status,
                ('juju', '--show-log', 'action', 'do', '-e', 'foo',
                 'chaos-monkey/1', 'start', 'mode=single',
                 'enablement-timeout=0', 'monkey-id=1234'
                 ): 'Action queued with id: abcd',
                }
            return output[args]
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo/juju')
        monkey_runner = MonkeyRunner('foo', client, service='jenkins')
        with patch('subprocess.check_output', side_effect=output,
                   autospec=True) as co_mock:
            returned = monkey_runner.unleash_once()
        expected = ['abcd', '1234']
        assert_juju_call(self, co_mock, client, (
            'juju', '--show-log', 'action', 'do', '-e', 'foo',
            'chaos-monkey/1', 'start', 'mode=single', 'enablement-timeout=0'),
            1, True)
        assert_juju_call(self, co_mock, client, (
            'juju', '--show-log', 'action', 'do', '-e', 'foo',
            'chaos-monkey/0', 'start', 'mode=single', 'enablement-timeout=0'),
            2, True)
        self.assertEqual(['chaos-monkey/1', 'chaos-monkey/0'],
                         monkey_runner.monkey_ids.keys())
        self.assertEqual(len(monkey_runner.monkey_ids), 2)
        self.assertEqual(co_mock.call_count, 3)
        self.assertItemsEqual(returned, expected)
        with patch('subprocess.check_output', side_effect=output,
                   autospec=True) as co_mock:
            monkey_runner.unleash_once()
        assert_juju_call(self, co_mock, client, (
            'juju', '--show-log', 'action', 'do', '-e', 'foo',
            'chaos-monkey/1', 'start', 'mode=single', 'enablement-timeout=0',
            'monkey-id=1234'), 1, True)
        assert_juju_call(self, co_mock, client, (
            'juju', '--show-log', 'action', 'do', '-e', 'foo',
            'chaos-monkey/0', 'start', 'mode=single', 'enablement-timeout=0',
            'monkey-id=abcd'), 2, True)
        self.assertTrue('1234', monkey_runner.monkey_ids['chaos-monkey/1'])
        # Test monkey_ids.get(unit_name) does not change on second call to
        # unleash_once()
        with patch('subprocess.check_output', side_effect=output2,
                   autospec=True):
            monkey_runner.unleash_once()
        self.assertEqual('1234', monkey_runner.monkey_ids['chaos-monkey/1'])

    def test_unleash_once_raises_for_unexpected_action_output(self):
        def output(args, **kwargs):
            status = yaml.safe_dump({
                'machines': {
                    '0': {'agent-state': 'started'}
                },
                'services': {
                    'jenkins': {
                        'units': {
                            'foo': {
                                'subordinates': {
                                    'chaos-monkey/0': {'baz': 'qux'},
                                }
                            }
                        }
                    }
                }
            })
            output = {
                ('juju', '--show-log', 'status', '-e', 'foo'): status,
                ('juju', '--show-log', 'action', 'do', '-e', 'foo',
                 'chaos-monkey/0', 'start', 'mode=single',
                 'enablement-timeout=0'
                 ): 'Action fail',
                }
            return output[args]
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo/juju')
        monkey_runner = MonkeyRunner('foo', client, service='jenkins')
        with patch('subprocess.check_output', side_effect=output,
                   autospec=True):
            with self.assertRaisesRegexp(
                    Exception, 'Unexpected output from "juju action do":'):
                monkey_runner.unleash_once()


class TestIsHealthy(TestCase):

    def test_is_healthy(self):
        SCRIPT = """#!/bin/bash\necho -n 'PASS'\nexit 0"""
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo/juju')
        with NamedTemporaryFile(delete=False) as health_script:
            health_script.write(SCRIPT)
            os.fchmod(health_script.fileno(), stat.S_IEXEC | stat.S_IREAD)
            health_script.close()
            monkey_runner = MonkeyRunner('foo', client,
                                         health_checker=health_script.name)
            with patch('logging.info') as lo_mock:
                result = monkey_runner.is_healthy()
            os.unlink(health_script.name)
            self.assertTrue(result)
            self.assertEqual(lo_mock.call_args[0][0],
                             'Health check output: PASS')

    def test_is_healthy_fail(self):
        SCRIPT = """#!/bin/bash\necho -n 'FAIL'\nexit 1"""
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo/juju')
        with NamedTemporaryFile(delete=False) as health_script:
            health_script.write(SCRIPT)
            os.fchmod(health_script.fileno(), stat.S_IEXEC | stat.S_IREAD)
            health_script.close()
            monkey_runner = MonkeyRunner('foo', client,
                                         health_checker=health_script.name)
            with patch('logging.error') as le_mock:
                result = monkey_runner.is_healthy()
            os.unlink(health_script.name)
            self.assertFalse(result)
            self.assertEqual(le_mock.call_args[0][0], 'FAIL')

    def test_is_healthy_with_no_execute_perms(self):
        SCRIPT = """#!/bin/bash\nexit 0"""
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo/juju')
        with NamedTemporaryFile(delete=False) as health_script:
            health_script.write(SCRIPT)
            os.fchmod(health_script.fileno(), stat.S_IREAD)
            health_script.close()
            monkey_runner = MonkeyRunner('foo', client,
                                         health_checker=health_script.name)
            with patch('logging.error') as le_mock:
                with self.assertRaises(OSError):
                    monkey_runner.is_healthy()
            os.unlink(health_script.name)
        self.assertRegexpMatches(
            le_mock.call_args[0][0],
            r'The health check failed to execute with: \[Errno 13\].*')


class TestWaitForChaos(TestCase):

    def test_wait_for_chaos_complete(self):
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo')
        runner = MonkeyRunner('foo', client)
        units = [('blib', 'blab')]
        with patch.object(runner, 'iter_chaos_monkey_units', autospec=True,
                          return_value=units) as ic_mock:
            with patch.object(runner, 'get_unit_status',
                              autospec=True, return_value='done') as us_mock:
                returned = runner.wait_for_chaos()
        self.assertEqual(returned, None)
        self.assertEqual(ic_mock.call_count, 1)
        self.assertEqual(us_mock.call_count, 1)

    def test_wait_for_chaos_complete_timesout(self):
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo')
        runner = MonkeyRunner('foo', client)
        with self.assertRaisesRegexp(
                Exception, 'Chaos operations did not complete.'):
            runner.wait_for_chaos(timeout=0)

    def test_wait_for_chaos_started(self):
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo')
        runner = MonkeyRunner('foo', client)
        units = [('blib', 'blab')]
        with patch.object(runner, 'iter_chaos_monkey_units', autospec=True,
                          return_value=units) as ic_mock:
            with patch.object(runner, 'get_unit_status',
                              autospec=True,
                              return_value='running') as us_mock:
                returned = runner.wait_for_chaos(state='start')
        self.assertEqual(returned, None)
        self.assertEqual(ic_mock.call_count, 1)
        self.assertEqual(us_mock.call_count, 1)

    def test_wait_for_chaos_unexpected_state(self):
        client = EnvJujuClient(SimpleEnvironment('foo', {}), None, '/foo')
        runner = MonkeyRunner('foo', client)
        with self.assertRaisesRegexp(
                Exception, 'Unexpected state value: foo'):
            runner.wait_for_chaos(state='foo')
