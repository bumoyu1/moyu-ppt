"""A scoring report needs observed decisions; a fixture is never upgraded to a run."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('routing_evidence', Path(__file__).resolve().parents[1] / 'scripts/evaluate_routing.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RoutingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.cases = [{'id': 'a', 'expected_skill': 'moyu-ppt', 'expected_stage': 'plans'}]
        self.row = {'id': 'a', 'skill': 'moyu-ppt', 'stage': 'plans', 'reason': 'Only the deck plan was requested.'}
        self.observed = {'evidence_type': 'agent_semantic_probe', 'runner': 'independent agent', 'decisions': [self.row]}

    def test_missing_observations_fail_instead_of_predicting(self):
        self.observed['decisions'] = []
        result = module.evaluate(self.cases, self.observed)
        self.assertFalse(result['ok'])
        self.assertIsNone(result['results'][0]['observed'])

    def test_right_skill_wrong_stage_is_failure(self):
        self.row['stage'] = 'deck'
        self.assertFalse(module.evaluate(self.cases, self.observed)['ok'])

    def test_duplicate_or_unknown_decisions_are_rejected(self):
        for rows in ([self.row, self.row], [{**self.row, 'id': 'unknown'}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                module.evaluate(self.cases, {**self.observed, 'decisions': rows})

    def test_duplicate_cases_cannot_shrink_denominator(self):
        with self.assertRaises(ValueError):
            module.evaluate(self.cases * 2, self.observed)

    def test_empty_provenance_and_rationale_are_rejected(self):
        for changed in ({'runner': ' '}, {'evidence_type': 'provider-backed'}, {'decisions': [{**self.row, 'reason': ' '}]}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                module.evaluate(self.cases, {**self.observed, **changed})

    def test_fixture_label_is_preserved_even_when_correct(self):
        result = module.evaluate(self.cases, {**self.observed, 'evidence_type': 'recorded_fixture'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['evidence_type'], 'recorded_fixture')
        self.assertIn('fixtures are not model runs', result['limitation'])


if __name__ == '__main__':
    unittest.main()
