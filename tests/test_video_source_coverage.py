"""Every admitted video source has one durable byte outcome in manifest v2."""
import copy
import unittest

from reproloop.core import ContractError
from reproloop.live.video_sources import validate_source_coverage


def source(sequence, native=None):
    return {"sequence": sequence,
            "acquisitionSequence": native if native is not None else sequence,
            "digest": f"{sequence:064x}"}


def frame(item):
    return {"recordingFrameSequence": item["sequence"],
            "acquisitionSequence": item["acquisitionSequence"],
            "sourceDigest": item["digest"]}


def loss(first, last, *, kind="captured-dropped", stage="queue"):
    return {"lossClass": kind, "stage": stage,
            "recordingFrameRange": {"first": first, "last": last}}


class VideoSourceCoverageTests(unittest.TestCase):
    def test_complete_sources_bind_to_each_durable_frame(self):
        sources = [source(1, 4), source(2, 5), source(3, 8)]
        segments = [{"frames": [frame(item) for item in sources[:2]]},
                    {"frames": [frame(sources[2])]}]
        validate_source_coverage(sources, segments, [])

    def test_missing_duplicate_or_rebound_source_is_rejected(self):
        sources = [source(1), source(2)]
        valid = [frame(item) for item in sources]
        malformed = [valid[:1], valid + [valid[0]]]
        for key, value in (("sourceDigest", "f" * 64),
                           ("acquisitionSequence", 4),
                           ("recordingFrameSequence", 3)):
            changed = copy.deepcopy(valid)
            changed[0][key] = value
            malformed.append(changed)
        for frames in malformed:
            with self.subTest(frames=frames), self.assertRaises(ContractError):
                validate_source_coverage(sources, [{"frames": frames}], [])

    def test_coalesced_losses_cover_only_their_admitted_range(self):
        sources = [source(i) for i in range(1, 5)]
        segments = [{"frames": [frame(sources[3])]}]
        validate_source_coverage(sources, segments, [loss(1, 3)])
        for losses in ([loss(1, 2)], [loss(1, 4)], [loss(1, 3), loss(2, 3)],
                       [loss(0, 3)], [loss(3, 1)], [loss(1, 5)]):
            with self.subTest(losses=losses), self.assertRaises(ContractError):
                validate_source_coverage(sources, segments, losses)

    def test_durable_sources_preserve_acquisition_order_across_segments(self):
        sources = [source(1), source(2)]
        first, second = [frame(item) for item in sources]
        for segments in ([{"frames": [second, first]}],
                         [{"frames": [second]}, {"frames": [first]}]):
            with self.subTest(segments=segments), self.assertRaises(ContractError):
                validate_source_coverage(sources, segments, [])

    def test_explicit_null_sequence_domains_are_invalid(self):
        sources = [source(1)]
        segments = [{"frames": [frame(sources[0])]}]
        for key in ("recordingFrameRange", "nativeSequenceRange"):
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate_source_coverage(sources, segments, [{
                    "lossClass": "unknown-acquisition-interval", "stage": "timing", key: None}])

    def test_unknown_timing_is_not_an_alternative_to_durable_bytes(self):
        sources = [source(1)]
        timing = loss(1, 1, kind="unknown-acquisition-interval", stage="timing")
        validate_source_coverage(sources, [{"frames": [frame(sources[0])]}], [timing])
        with self.assertRaises(ContractError):
            validate_source_coverage(sources, [], [timing])

    def test_native_gaps_cannot_claim_an_admitted_source(self):
        sources = [source(1, 1), source(2, 4)]
        segments = [{"frames": [frame(item) for item in sources]}]
        gap = {"lossClass": "captured-dropped", "stage": "transport",
               "nativeSequenceRange": {"first": 2, "last": 3}}
        validate_source_coverage(sources, segments, [gap])
        for first, last in ((1, 3), (2, 4), (0, 2), (3, 2)):
            altered = copy.deepcopy(gap)
            altered["nativeSequenceRange"] = {"first": first, "last": last}
            with self.subTest(first=first, last=last), self.assertRaises(ContractError):
                validate_source_coverage(sources, segments, [altered])
        with self.assertRaises(ContractError):
            validate_source_coverage(sources, segments, [gap, gap])

    def test_ledger_order_identity_and_exact_fields_are_checked(self):
        bad_ledgers = [[source(2)], [source(1), source(2, 1)],
                       [{**source(1), "path": "source.jpg"}],
                       [{**source(1), "sequence": True}],
                       [{**source(1), "digest": "bad"}]]
        for sources in bad_ledgers:
            with self.subTest(sources=sources), self.assertRaises(ContractError):
                validate_source_coverage(sources, [], [loss(1, len(sources))])

    def test_ten_thousand_sources_can_be_covered_without_raw_object_references(self):
        sources = [source(i) for i in range(1, 10_001)]
        segments = [{"frames": [frame(item) for item in sources[i:i + 150]]}
                    for i in range(0, len(sources), 150)]
        validate_source_coverage(sources, segments, [])
        with self.assertRaises(ContractError):
            validate_source_coverage(sources + [source(10_001)], segments, [])


if __name__ == "__main__":
    unittest.main()
