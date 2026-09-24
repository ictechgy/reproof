"""Interrupted fixed signing factories must close their partial ownership."""
from contextlib import ExitStack
from unittest.mock import patch

from reproof.repair_signing import TrustedSigningSupervisor


def check_interrupted_factory(case, configure, *, owner, operations_type,
                              signer_type, inspector_type, resolver):
    for phase in ('inspector', 'ready'):
        for interruption in (KeyboardInterrupt, SystemExit):
            with case.subTest(phase=phase, interruption=interruption.__name__):
                created = []

                def observe(original):
                    def initialize(instance, *args, **kwargs):
                        original(instance, *args, **kwargs)
                        created.append(instance)
                    return initialize

                def interrupt(*_args, **_kwargs):
                    raise interruption()

                with ExitStack() as stack:
                    for kind in (operations_type, signer_type, inspector_type):
                        stack.enter_context(patch.object(kind, '__init__', observe(kind.__init__)))
                    target, method = ((inspector_type, '__init__') if phase == 'inspector'
                                      else (TrustedSigningSupervisor, 'ready'))
                    stack.enter_context(patch.object(target, method, interrupt))
                    stack.enter_context(patch.object(resolver, 'open',
                        side_effect=AssertionError('Construction must not read private key contents')))
                    with case.assertRaises(interruption):
                        configure()
                case.assertEqual(owner.status()['cleanupPending'], 0)
                case.assertFalse(resolver._closed, 'Unadopted material still belongs to the caller')
                case.assertEqual(len(created), 2 if phase == 'inspector' else 3)
                case.assertTrue(all(item._closed for item in created),
                                'An interrupted factory left a partial owner open')
