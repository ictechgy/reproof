"""G9 fixtures use real G4 stores/loopback preparation and explicit synthetic providers."""
from unittest import mock

from reproloop.project_repair import RepairSource
from reproloop.qualification import QualificationEngine
from tests.g4_support import G4Environment
from tests.test_project_repair import source_fixture


class RepairEnvironment(G4Environment):
    def __init__(self):
        project, self.source_blobs, self.original_artifacts = source_fixture()
        with mock.patch('tests.g4_support.project_document', return_value=project):
            super().__init__(recording_capacity_bytes=512*1024*1024)
        self.root = self.root.resolve()
        self.source_blobs.write_new(self.root / 'source')
        self.original_artifacts.write_new(self.root / 'original-build')
        self.lab.devices['device']['capabilities']['applicationIdentity']['artifactDigest'] = project['builds'][0]['artifactDigest']
        self.source = RepairSource(project, self.root / 'source',
            source_paths=tuple(path for path, _ in self.source_blobs.entries),
            protected_paths=('tests/CheckoutTests.swift',),
            original_artifact_root=self.root / 'original-build',
            original_artifact_paths=('original.bin',))
        self.original = self.record_original()
        self.approved = self.approve(self.original)
        self.engine = QualificationEngine(self.root / 'baseline', self.registry)

    def baseline(self, *, campaign_id='original_baseline'):
        return self.engine.run_original(self.approved, lambda _number: self.service.replay(
            self.registry.original_execution(self.approved), registration=self.registration,
            device_id='device', owner='owner', controller_id='baseline', preparations=self.preparations()),
            campaign_id=campaign_id)

    def close(self):
        self.engine.close()
        super().close()
