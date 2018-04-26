import os
import unittest

from mensor.measures.registry import MeasureRegistry
from mensor.providers.pandas import PandasMeasureProvider
from mensor.measures.context import BaseConstraint, Constraint
from mensor.measures.strategy import STRATEGY_TYPE


class EvaluationStrategyTests(unittest.TestCase):

    def setUp(self):
        self.registry = MeasureRegistry()

        data_dir = os.path.join(os.path.dirname(__file__), 'data')

        people = (
            PandasMeasureProvider(
                name='people',
                data=os.path.join(data_dir, 'people.csv')
            )
            .add_identifier('person', expr='id', role='primary')
            .add_dimension('name')
            .add_measure('age')
            .add_partition('ds')
        )
        self.registry.register(people)

        people2 = (
            PandasMeasureProvider(
                name='people2',
                data=os.path.join(data_dir, 'people.csv')
            )
            .add_identifier('person', expr='id', role='unique')
            .add_dimension('geography', expr='id_country')
            .add_partition('ds')
        )
        self.registry.register(people2)

        transactions = (
            PandasMeasureProvider(
                name='transactions',
                data=os.path.join(data_dir, 'transactions.csv')
            )
            .add_identifier('transaction', expr='id', role='primary')
            .add_identifier('person:buyer', expr='id_buyer', role='foreign')
            .add_identifier('person:seller', expr='id_seller', role='foreign')
            .add_measure('value')
            .add_partition('ds', requires_constraint=True)
        )
        self.registry.register(transactions)

        self.registry.show()

    def test_simple(self):
        es = self.registry.evaluate('person', measures=['age'], segment_by=['name'], dry_run=True)

        self.assertIn('age', es.measures)
        self.assertIn('name', es.segment_by)

    def test_simple_where(self):
        es = self.registry.evaluate('person', measures=['age'], segment_by=['name'],
                                    where={'ds': '2018-01-01'}, dry_run=True)

        self.assertIn('age', es.measures)
        self.assertIn('name', es.segment_by)
        self.assertIn('ds', es.segment_by)
        self.assertIsInstance(es.where, Constraint)
        self.assertEqual(es.where.field, 'ds')
        self.assertEqual(es.where.value, '2018-01-01')
        self.assertEqual(es.where.relation, '==')

    @unittest.skip("Constraints are not currently enforced if not present in query.")
    def test_partition_constraint_enforced(self):
        self.assertRaises(RuntimeError, self.registry.evaluate, 'transaction',
                          measures=['value'], dry_run=True)

        # The following should not fail
        self.registry.evaluate('transaction', measures=['value'], where={'ds': '2018-01-01'}, dry_run=True)

    def test_multiple_providers_for_unit_type(self):
        es = self.registry.evaluate(
            'person:seller',
            measures=['age'],
            segment_by=['geography'],
            dry_run=True
        )

        self.assertIn('age', es.measures)
        self.assertIn('geography', es.segment_by)
        # self.assertFalse(es.segment_by['geography'].private)
        # self.assertTrue(es.segment_by['geography'].external)
        self.assertIn('person', es.segment_by)
        # self.assertTrue(es.segment_by['person'].private)
        self.assertIn('ds', es.segment_by)

        self.assertEqual(es.joins[0].provider.name, 'people2')

        self.assertEqual(set(es.joins[0].join_on_left), set(['person', 'ds']))
        self.assertEqual(set(es.joins[0].join_on_right), set(['person', 'ds']))

        self.assertIsNone(es.joins[0].join_prefix)

    def test_forward_joins(self):
        es = self.registry.evaluate('transaction', measures=['person:seller/age'], segment_by=['person:buyer/name'],
                                    where={'*/ds': '2018-01-01'}, dry_run=True)

        # TODO: uncomment privacy when strategy updated to use dictionaries

        # Top-level strategy check
        self.assertIn('person:seller/age', es.measures)
        # self.assertFalse(es.measures['person:seller/age'].private)
        self.assertIn('person:buyer/name', es.segment_by)
        # self.assertFalse(es.segment_by['person:buyer/age'].private)
        self.assertIn('person:seller', es.segment_by)
        # self.assertTrue(es.segment_by['person:seller/person'].private)
        self.assertIn('person:buyer', es.segment_by)
        # self.assertTrue(es.segment_by['person:buyer/person'].private)
        self.assertIn('ds', es.segment_by)
        # self.assertTrue(es.segment_by['person:buyer/ds'].private)

        # Joins check
        self.assertEqual(len(es.joins), 2)

        for join in es.joins:
            self.assertEqual({'person', 'ds'}, set(join.join_on_right))
            if join.unit_type == 'person:seller':
                self.assertEqual({'person:seller', 'ds'}, set(join.join_on_left))
            elif join.unit_type == 'person:buyer':
                self.assertEqual({'person:buyer', 'ds'}, set(join.join_on_left))
            else:
                raise ValueError("Invalid unit type detected.")

    def test_reverse_joins(self):
        es = self.registry.evaluate('person:seller', measures=['transaction/value'], segment_by=['name'],
                                    where={'*/ds': '2018-01-01'}, dry_run=True)

        # TODO: uncomment privacy when strategy updated to use dictionaries

        self.assertIn('transaction/value', es.measures)
        # self.assertFalse(es.segment_by['transaction/value'].private)
        # self.assertTrue(es.segment_by['transaction/value'].external)
        self.assertIn('name', es.segment_by)
        # self.assertFalse(es.segment_by['name'].private)

        # Join information
        rjoin = es.joins[0]
        self.assertEqual(rjoin.join_prefix, 'transaction')
        self.assertEqual(rjoin.unit_type, 'person:seller')
        self.assertEqual(rjoin.strategy_type, STRATEGY_TYPE.UNIT_REBASE)
        self.assertIn('person:seller', rjoin.segment_by)
        self.assertIn('ds', rjoin.segment_by)
        self.assertEqual({'person', 'ds'}, set(rjoin.join_on_left))
        self.assertEqual({'person:seller', 'ds'}, set(rjoin.join_on_right))