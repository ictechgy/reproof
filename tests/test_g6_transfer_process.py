"""Actual two-process recovery probe while an admitted chunk is still writing."""
from __future__ import annotations
import hashlib
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from reproof.live.artifact_transfer import ArtifactTransferStore
from reproof.live.disk_budget import DiskBudget
from reproof.live.evidence_store import EvidenceStore

HOST = ('parent_mac', 1, 'parent_incarnation')


def open_stores(root, opened):
    root = Path(root)
    opened['budget'] = budget = DiskBudget(
        root / 'budget', capacity_bytes=16 * 1024 * 1024,
        journal_headroom_bytes=512 * 1024)
    opened['evidence'] = evidence = EvidenceStore(root / 'evidence', budget)
    opened['transfer'] = ArtifactTransferStore(
        root / 'transfer', budget, evidence, object_quota_bytes=1024 * 1024,
        project_quota_bytes=4 * 1024 * 1024, host_quota_bytes=4 * 1024 * 1024)


def close_stores(opened):
    for name in ('transfer', 'evidence', 'budget'):
        if name in opened:
            opened[name].close()


def chunk_writer(root, channel):
    opened = {}
    result = {}
    try:
        open_stores(root, opened)
        transfer = opened['transfer']
        body = b'owned in-flight chunk'
        digest = hashlib.sha256(body).hexdigest()
        upload = transfer.allocate(
            project_id='parent_project', host_identity=HOST, kind='manifest',
            size=len(body), digest=digest, metadata={'format': 'json'},
            retention_class='original', retain_until_ms=int(time.time() * 1000) + 60000,
            authorizer=lambda _: True)
        original = os.pwrite

        def held(descriptor, data, offset):
            channel.send({'phase': 'held-before-write', 'objectId': upload['objectId']})
            if not channel.poll(8) or channel.recv() != 'release':
                raise RuntimeError('Owned chunk probe timed out')
            return original(descriptor, data, offset)

        with mock.patch.object(os, 'pwrite', held):
            chunk = transfer.put_chunk(upload['objectId'], upload['uploadGeneration'],
                                       0, body, digest, host_identity=HOST,
                                       authorizer=lambda _: True)
        result['chunkState'] = chunk['state']
        published = transfer.finalize(upload['objectId'], upload['uploadGeneration'],
                                      host_identity=HOST, authorizer=lambda _: True)
        result['publicationState'] = published['state']
    except Exception as exc:
        result['exceptionType'] = type(exc).__name__
        result['errorCode'] = getattr(exc, 'code', None)
    finally:
        close_stores(opened)
        try:
            channel.send({'phase': 'finished', **result})
        finally:
            channel.close()


def probe():
    context = multiprocessing.get_context('spawn')
    with tempfile.TemporaryDirectory(prefix='g6-parent-process-') as directory:
        parent_channel, child_channel = context.Pipe()
        child = context.Process(target=chunk_writer, args=(directory, child_channel))
        opened = {}
        observed = {}
        opener_done = threading.Event()
        opener = None
        child.start()
        child_channel.close()
        try:
            if not parent_channel.poll(8):
                raise RuntimeError('Owned writer did not reach its boundary')
            ready = parent_channel.recv()
            if ready.get('phase') != 'held-before-write':
                raise RuntimeError('Owned writer failed before the held boundary')

            def open_concurrently():
                try:
                    open_stores(directory, opened)
                    observed['stateOnConcurrentOpen'] = opened['transfer'].status(
                        ready['objectId'], host_identity=HOST,
                        authorizer=lambda _: True)['state']
                except Exception as exc:
                    observed['exceptionType'] = type(exc).__name__
                    observed['errorCode'] = getattr(exc, 'code', None)
                finally:
                    opener_done.set()

            opener = threading.Thread(target=open_concurrently, daemon=True)
            opener.start()
            returned_while_held = opener_done.wait(.3)
            parent_channel.send('release')
            if not parent_channel.poll(8):
                raise RuntimeError('Owned writer did not return after release')
            finished = parent_channel.recv()
            child.join(5)
            opener.join(5)
            return {
                'scope': 'actual owned child process and parent store; writer held before pwrite',
                'secondOpenReturnedWhileWriterWasHeld': returned_while_held,
                'secondOpen': observed, 'writerResult': finished,
                'childExitCode': child.exitcode, 'openerFinished': not opener.is_alive(),
                'passed': (child.exitcode == 0 and not opener.is_alive()
                           and finished.get('chunkState') == 'uploading'
                           and finished.get('publicationState') == 'published'),
            }
        finally:
            if child.is_alive():
                child.terminate()
                child.join(3)
            if opener is not None:
                opener.join(2)
            close_stores(opened)
            parent_channel.close()



class TransferProcessRecoveryTests(unittest.TestCase):
    def test_concurrent_opener_cannot_reconcile_a_live_chunk_writer(self):
        result = probe()
        self.assertTrue(result['passed'], result)
