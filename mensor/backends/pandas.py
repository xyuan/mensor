import functools
import logging

import pandas as pd

from mensor.constraints import CONSTRAINTS
from mensor.measures.provider import MeasureProvider
from mensor.measures.stats import global_stats_registry
from mensor.measures.types import _Measure


class PandasMeasureProvider(MeasureProvider):
    # The base MeasureProvider.evaluate method requires the ability to interact
    # with pandas dataframes, and so some of the functionality of this class is
    # exposed as classmethods for use externally.

    REGISTRY_KEYS = ['pandas']

    @classmethod
    def register_stats(cls, key):
        register_pandas_agg = functools.partial(global_stats_registry.aggregations.register, backend=key)
        sum_agg = functools.partial(pd.Series.sum, min_count=1)
        register_pandas_agg('sum', agg=(sum_agg, lambda x: x))
        register_pandas_agg('mean', agg=('mean', lambda x: x))
        register_pandas_agg('sos', agg=(sum_agg, lambda x: x**2))
        register_pandas_agg('count', agg=(sum_agg, lambda x: x.notnull().astype(int)))

    def __init__(self, name, data=None, data_transform=None, **kwargs):
        MeasureProvider.__init__(self, name, **kwargs)
        if isinstance(data, str):
            data = pd.read_csv(data)
        self._data = data
        self._data_transform = data_transform

        self.provides_measure('count', shared=True, distribution=None, default=0)

    @property
    def data(self):
        if self._data_transform is None:
            return self._data
        return data_transform(self, self._data, self.provisions)

    def _evaluate(self, unit_type, measures, where=None, segment_by=None,
                  stats_registry=None, stats=True, covariates=False, **opts):

        assert stats_registry is not None
        assert not any(measure.external for measure in measures)
        assert not any(dimension.external for dimension in segment_by)
        rebase_agg = not unit_type.is_unique

        raw_data = (
            self.data
            .assign(count=1)
        )

        df = (
            pd.DataFrame()
            .assign(**{
                dimension.fieldname(role='dimension', unit_type=unit_type if not rebase_agg else None): raw_data.eval(dimension.expr) for dimension in segment_by
            })
            .assign(**{
                measure.fieldname(role='measure', unit_type=unit_type if not rebase_agg else None): raw_data.eval(measure.expr) for measure in measures
            })
        )

        return (
            self._finalise_dataframe(
                df, unit_type=unit_type, measures=measures, segment_by=segment_by, where=where,
                stats_registry=stats_registry, stats=stats, rebase_agg=rebase_agg
            )
        )

    @classmethod
    def _finalise_dataframe(cls, df, unit_type, measures, segment_by, where, stats_registry=None,
                            stats=False, rebase_agg=True, reagg=False):
        """
        This method finalises a `pandas.DataFrame` instance by applying the
        following steps:

        0) Apply any defaults as required.
        1) Apply any constraints provided in `where`.
        2) Ensuring that all private measures and dimensions are removed.
        3) Performing an aggregation over the measures segmented by the features
           identified in `segment_by`. This is done in one of two ways:
           A) if `rebase_agg` is `True`, a unit aggregation is performed.
           B) otherwise, a regular (summed) aggregation is performed. If `stats`
              is `True`, statistics about this aggregation are retained.
        4) If a unit aggregation was performed, and `stats` is True, repeat the
           aggregation to get (trivial) statistics.

        This method is implemented as a classmethod to make it accessible to
        `MeasureProvider.evaluate`, which provides the generic implementation
        for incompatible `MeasureProvider` instances using pandas DataFrames.
        """

        # Apply defaults, if required
        for measure in measures:
            if measure.default is not None:
                df[measure.fieldname(role='measure', unit_type=unit_type if not rebase_agg else None)].fillna(measure.default, inplace=True)

        for dimension in segment_by:
            if dimension.default is not None:
                df[dimension.fieldname(role='dimension', unit_type=unit_type if not rebase_agg else None)].fillna(dimension.default, inplace=True)

        # Apply constraints
        if where:
            df = cls._apply_where_to_df(df, where)

        # Remove any private measures and segments
        for measure in measures:
            if measure.private:
                df.drop(list(measure.get_fields(unit_type=unit_type, stats=False, stats_registry=stats_registry)), axis=1)
        for dimension in segment_by:
            if dimension.private:
                df.drop(dimension.via_name, axis=1)

        measures = [m for m in measures if not m.private]
        segment_by = [s for s in segment_by if not s.private]

        if rebase_agg:
            df = cls._dataframe_agg(df, unit_type, measures, segment_by, rebase_agg=True, stats_registry=stats_registry, stats=False)

        if not rebase_agg or stats:
            df = cls._dataframe_agg(df, unit_type, measures, segment_by, rebase_agg=False, stats_registry=stats_registry, stats=stats, reagg=reagg)

        return df

    @classmethod
    def _dataframe_agg(cls, df, unit_type, measures, segment_by, rebase_agg=False,
                       stats_registry=None, stats=False, reagg=False):

        measure_cols = _Measure.get_all_fields(measures, unit_type=unit_type, rebase_agg=rebase_agg, stats=stats, stats_registry=stats_registry)
        segment_by_cols = [s.fieldname(role='dimension', unit_type=unit_type if not rebase_agg else None) for s in segment_by]

        if len(df) == 0:
            return pd.DataFrame([], columns=measure_cols + segment_by_cols)

        measure_cols, measure_aggs = cls._measure_agg_maps(
            unit_type, measures, external=True, rebase_agg=rebase_agg, stats=stats, stats_registry=stats_registry, reagg=reagg
        )

        if isinstance(df, pd.Series):
            df = df.to_frame().T

        if len(segment_by_cols) > 0 and any([df[dimension].hasnans for dimension in segment_by_cols]):
            logging.warning(
                "The pandas backend currently drops null values from the "
                "groupby index, and null values were found in the "
                "segmentation fields: {}".format(segment_by_cols)
            )

        if len(segment_by_cols) > 0 and len(measure_cols) > 0:
            df = (
                df
                .assign(**measure_cols)
                [segment_by_cols + list(measure_cols)]
                .groupby(segment_by_cols)
                .agg(measure_aggs)
                .reset_index()
            )
        elif len(segment_by_cols) > 0:
            df = (
                df
                .assign(dummy=1)
                .groupby(segment_by_cols)
                .sum()
                .reset_index()
                [segment_by_cols]
            )
        else:
            df = df.assign(**measure_cols)[list(measure_cols)].agg(measure_aggs)

        return df

    # Aggregation related methods
    @classmethod
    def _measure_agg_maps(cls, unit_type, measures, external=True, rebase_agg=False, stats=False, stats_registry=None, reagg=False):
        def measure_map(name, op):
            return lambda df: op(df[name])
        col_maps = {}
        col_aggs = {}
        for measure in measures:
            if not external and measure.external:
                continue
            for field_name, (col_agg, col_map) in measure.get_fields(unit_type=unit_type, stats=stats, rebase_agg=rebase_agg, stats_registry=stats_registry, for_pandas=True).items():
                col_aggs[field_name] = functools.partial(pd.Series.sum, min_count=1) if reagg else col_agg
                col_maps[field_name] = measure_map(measure.prev_fieldname(role='measure') or measure.fieldname(role='measure', unit_type=unit_type if not rebase_agg else None), (lambda x: x) if reagg else col_map)
        return col_maps, col_aggs

    #  Constraint related methods
    @classmethod
    def _apply_where_to_df(cls, df, where):
        return df[cls._get_constraint_for_df(df, where)]

    @classmethod
    def _get_constraint_for_df(cls, df, constraint):
        try:
            return cls._get_constraint_maps()[constraint.kind](df, constraint)
        except:
            raise

    @classmethod
    def _get_constraint_maps(cls):
        """
        All constraints expect two parameters:
         - the DataFrame to be constrained
         - the constraint
        """
        from functools import reduce
        from operator import eq, gt, ge, lt, le, and_, or_
        return {
            CONSTRAINTS.AND: lambda df, c: reduce(and_, (cls._get_constraint_for_df(df, op) for op in c.operands)),
            CONSTRAINTS.OR: lambda df, c: reduce(or_, (cls._get_constraint_for_df(df, op) for op in c.operands)),
            CONSTRAINTS.EQUALITY: lambda df, c: eq(df[c.field], c.value),
            CONSTRAINTS.INEQUALITY_GT: lambda df, c: gt(df[c.field], c.value),
            CONSTRAINTS.INEQUALITY_GTE: lambda df, c: ge(df[c.field], c.value),
            CONSTRAINTS.INEQUALITY_LT: lambda df, c: lt(df[c.field], c.value),
            CONSTRAINTS.INEQUALITY_LTE: lambda df, c: le(df[c.field], c.value),
            CONSTRAINTS.IN: lambda df, c: df[c.field].isin(c.value),
        }

    @property
    def _constraint_maps(self):
        return self._get_constraint_maps()
