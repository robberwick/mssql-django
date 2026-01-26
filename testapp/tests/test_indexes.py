import logging
from collections import namedtuple

import django.db
from django import VERSION
from django.apps import apps
from django.db import models, migrations
from django.db.migrations.migration import Migration
from django.db.migrations.state import ProjectState
from django.db.models import UniqueConstraint
from django.db.utils import DEFAULT_DB_ALIAS, ConnectionHandler, ProgrammingError
from django.test import TestCase, TransactionTestCase
from unittest import skipIf, expectedFailure

from . import get_constraints
from ..models import (
    TestIndexesRetainedRenamed,
    Choice,
    Question,
)

connections = ConnectionHandler()

if (VERSION >= (3, 2)):
    from django.utils.connection import ConnectionProxy
    connection = ConnectionProxy(connections, DEFAULT_DB_ALIAS)
else:
    from django.db import DefaultConnectionProxy
    connection = DefaultConnectionProxy()

logger = logging.getLogger('mssql.tests')

# Result type for migration test helper
MigrationTestResult = namedtuple('MigrationTestResult', ['model', 'constraints', 'project_state'])


class TestIndexesRetained(TestCase):
    """
    Issue https://github.com/microsoft/mssql-django/issues/14
    Indexes dropped during a migration should be re-created afterwards
    assuming the field still has `db_index=True`
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Pre-fetch which indexes exist for the relevant test model
        # now that all the test migrations have run
        cls.constraints = get_constraints(table_name=TestIndexesRetainedRenamed._meta.db_table)
        cls.indexes = {k: v for k, v in cls.constraints.items() if v['index'] is True}

    def _assert_index_exists(self, columns):
        matching = {k: v for k, v in self.indexes.items() if set(v['columns']) == columns}
        assert len(matching) == 1, (
            "Expected 1 index for columns %s but found %d %s" % (
                columns,
                len(matching),
                ', '.join(matching.keys())
            )
        )

    def test_field_made_nullable(self):
        # case (a) of https://github.com/microsoft/mssql-django/issues/14
        self._assert_index_exists({'a'})

    def test_field_renamed(self):
        # case (b) of https://github.com/microsoft/mssql-django/issues/14
        self._assert_index_exists({'b_renamed'})

    def test_table_renamed(self):
        # case (c) of https://github.com/microsoft/mssql-django/issues/14
        self._assert_index_exists({'c'})

def _get_all_models():
    for app in apps.get_app_configs():
        app_label = app.label
        for model_name, model_class in app.models.items():
            yield model_class, model_name, app_label


class TestCorrectIndexes(TestCase):

    def test_correct_indexes_exist(self):
        """
        Check there are the correct number of indexes for each field after all migrations
        by comparing what the model says (e.g. `db_index=True` / `index_together` etc.)
        with the actual constraints found in the database.
        This acts as a general regression test for issues such as:
         - duplicate index created (e.g. https://github.com/microsoft/mssql-django/issues/77)
         - index dropped but accidentally not recreated
         - index incorrectly 'recreated' when it was never actually dropped or required at all
        Note of course that it only covers cases which exist in testapp/models.py and associated migrations
        """
        connection = django.db.connections[django.db.DEFAULT_DB_ALIAS]
        for model_cls, model_name, app_label in _get_all_models():
            logger.debug('Checking model: %s.%s', app_label, model_name)
            if not model_cls._meta.managed:
                # Models where the table is not managed by Django migrations are irrelevant
                continue
            model_constraints = get_constraints(table_name=model_cls._meta.db_table)
            # Check correct indexes are in place for all fields in model
            for field in model_cls._meta.get_fields():
                if not hasattr(field, 'column'):
                    # ignore things like reverse fields which don't have a column on this table
                    continue
                col_name = connection.introspection.identifier_converter(field.column)
                field_str = f'{app_label}.{model_name}.{field.name} ({col_name})'
                logger.debug('  > Checking field: %s', field_str)

                # Find constraints which include this column
                col_constraints = [
                    dict(name=name, **infodict) for name, infodict in model_constraints.items()
                    if col_name in infodict['columns']
                ]
                col_indexes = [c for c in col_constraints if c['index']]
                for c in col_constraints:
                    logger.debug('    > Column <%s> is involved in constraint: %s', col_name, c)

                # There should be an explicit index for each of the following cases
                expected_index_causes = []
                if field.db_index:
                    expected_index_causes.append('db_index=True')
                if VERSION < (5, 1):
                   for field_names in model_cls._meta.index_together:
                      if field.name in field_names:
                         expected_index_causes.append(f'index_together[{field_names}]')
                if field._unique and field.null:
                    # This is implemented using a (filtered) unique index (not a constraint) to get ANSI NULL behaviour
                    expected_index_causes.append('unique=True & null=True')
                for field_names in model_cls._meta.unique_together:
                    if field.name in field_names:
                        # unique_together results in an index because this backend implements it using a
                        # (filtered) unique index rather than a constraint, to get ANSI NULL behaviour
                        expected_index_causes.append(f'unique_together[{field_names}]')
                for uniq_constraint in filter(lambda c: isinstance(c, UniqueConstraint), model_cls._meta.constraints):
                    if field.name in uniq_constraint.fields and uniq_constraint.condition is not None:
                        # Meta:constraints > UniqueConstraint with condition are implemented with filtered unique index
                        expected_index_causes.append(f'UniqueConstraint (with condition) in Meta: constraints')

                # Other cases like `unique=True, null=False` or `field.primary_key` do have index-like constraints
                # but in those cases the introspection returns `"index": False` so they are not in the list of
                # explicit indexes which we are checking here (`col_indexes`)

                assert len(col_indexes) == len(expected_index_causes), \
                    'Expected %s index(es) on %s but found %s.\n' \
                    'Check for behaviour changes around index drop/recreate in methods like _alter_field.\n' \
                    'Expected due to: %s\n' \
                    'Found: %s' % (
                        len(expected_index_causes),
                        field_str,
                        len(col_indexes),
                        expected_index_causes,
                        '\n'.join(str(i) for i in col_indexes),
                    )
                logger.debug('  Found %s index(es) as expected', len(col_indexes))


class TestIndexesBeingDropped(TestCase):

    def test_unique_index_dropped(self):
        """
        Issues https://github.com/microsoft/mssql-django/issues/110
        and https://github.com/microsoft/mssql-django/issues/90
        Unique indexes not being dropped when changing non-nullable
        foreign key with unique_together to nullable causing
        dependent on column error
        """
        old_field = Choice._meta.get_field('question')
        new_field = models.ForeignKey(
            Question, null=False, on_delete=models.deletion.CASCADE
        )
        new_field.set_attributes_from_name("question")
        with connection.schema_editor() as editor:
            editor.alter_field(Choice, old_field, new_field, strict=True)

        old_field = new_field
        new_field = models.ForeignKey(
            Question, null=True, on_delete=models.deletion.CASCADE
        )
        new_field.set_attributes_from_name("question")
        try:
            with connection.schema_editor() as editor:
                editor.alter_field(Choice, old_field, new_field, strict=True)
        except ProgrammingError:
            self.fail("Unique indexes not being dropped")

class TestMetaIndexesRetained(TransactionTestCase):
    """
    Regression test for indexes defined via Meta.indexes being dropped
    and not recreated after altering one of the indexed columns.

    Tests various schema operations that trigger index drop/recreate logic to ensure
    indexes are properly restored.

    Each test runs twice:
    - With migrations in split contexts (simulates separate migration files)
    - With migrations in combined context (simulates single migration file with multiple operations)
    """

    def _run_migration_test(
        self,
        MigrationA: type[Migration],
        MigrationB: type[Migration],
        migration_name_prefix: str,
        model_name: str,
        use_single_context: bool,
    ) -> MigrationTestResult:
        """
        Helper to run migration tests with either combined or split schema_editor contexts.

        Args:
            MigrationA: Migration class for initial setup (CreateModel + AddIndex)
            MigrationB: Migration class for the alteration being tested
            migration_name_prefix: Prefix for migration names (e.g., 'test_mc_type')
            model_name: Name of the model being tested
            use_single_context: If True, apply both migrations in one schema_editor context

        Returns:
            MigrationTestResult: Named tuple containing (model, constraints, project_state)
        """
        # Use django.db.connections to get a fresh connection for TransactionTestCase
        conn = django.db.connections[django.db.DEFAULT_DB_ALIAS]
        suffix = '_combined' if use_single_context else '_split'
        migration_a = MigrationA(name=f'{migration_name_prefix}{suffix}_a', app_label='testapp')
        migration_b = MigrationB(name=f'{migration_name_prefix}{suffix}_b', app_label='testapp')

        if use_single_context:
            # Combined: both migrations in one schema_editor context
            # This simulates combining operations in a single migration file
            with conn.schema_editor(atomic=True) as editor:
                project_state = migration_a.apply(ProjectState(), editor)
                project_state = migration_b.apply(project_state, editor)
        else:
            # Split: each migration in its own schema_editor context
            # This simulates two separate migration files
            with conn.schema_editor(atomic=True) as editor:
                project_state = migration_a.apply(ProjectState(), editor)
            with conn.schema_editor(atomic=True) as editor:
                project_state = migration_b.apply(project_state, editor)

        # Get the model and constraints for assertions
        model = project_state.apps.get_model('testapp', model_name)
        constraints = get_constraints(table_name=model._meta.db_table)

        return MigrationTestResult(model, constraints, project_state)

    def _assert_index_exists(self, constraints, expected_columns, error_msg):
        """
        Assert that an index with exactly the expected columns exists.

        Args:
            constraints: Dictionary of constraints from get_constraints()
            expected_columns: Set of column names that should be in the index
            error_msg: Message to display if assertion fails
        """
        found = any(
            set(info['columns']) == expected_columns and info['index']
            for info in constraints.values()
        )
        self.assertTrue(found, error_msg)

    def _get_context_description(self, use_single_context: bool) -> str:
        return "combined context" if use_single_context else "split contexts"

    def test_index_from_meta_indexes_retained_after_type_change(self):
        """
        Test that indexes defined in _meta.indexes are retained when altering field type (max_length change).
        This exercises the type change code path in _alter_field.
        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaIdxType{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20)),
                                ('b', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'b'], name=f'idx_type{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='a',
                            field=models.CharField(max_length=40),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_type',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'b'},
                    error_msg=(
                        f"Index on ('a', 'b') from _meta.indexes was not recreated after field type change "
                        f"({self._get_context_description(use_single_context)}). Expected index to be restored after ALTER COLUMN operation."
                    ),
                )

    def test_index_from_meta_indexes_retained_after_nullability_change(self):
        """
        Test that indexes defined in _meta.indexes are retained when changing field nullability.
        This exercises the nullability change code path in _alter_field.
        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaIdxNull{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20)),
                                ('b', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'b'], name=f'idx_null{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='b',
                            field=models.CharField(max_length=20, null=True),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_null',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'b'},
                    error_msg=(
                        f"Index on ('a', 'b') from _meta.indexes was not recreated after nullability change "
                        f"({self._get_context_description(use_single_context)}). Expected index to be restored after ALTER COLUMN NULL operation."
                    ),
                )

    def test_index_from_meta_indexes_retained_after_field_rename(self):
        """
        Test that indexes defined in _meta.indexes are retained and updated when renaming a field.
        The index should exist on the renamed column.
        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaIdxRename{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20)),
                                ('b', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'b'], name=f'idx_rename{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.RenameField(
                            model_name=model_name.lower(),
                            old_name='a',
                            new_name='a_renamed',
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_rename',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a_renamed', 'b'},
                    error_msg=(
                        f"Index on ('a_renamed', 'b') from _meta.indexes was not found after field rename "
                        f"({self._get_context_description(use_single_context)}). Expected index to be updated to reflect the renamed column."
                    ),
                )

    def test_index_from_meta_indexes_retained_after_altering_both_fields(self):
        """
        Test that indexes defined in _meta.indexes are retained when altering multiple fields in the index.
        This ensures the index is properly restored even when both participating columns are altered.
        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaIdxBoth{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20)),
                                ('b', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'b'], name=f'idx_both{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='a',
                            field=models.CharField(max_length=40),
                        ),
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='b',
                            field=models.CharField(max_length=30),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_both',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'b'},
                    error_msg=(
                        f"Index on ('a', 'b') from _meta.indexes was not recreated after altering both fields "
                        f"({self._get_context_description(use_single_context)}). Expected index to be restored after multiple ALTER COLUMN operations."
                    ),
                )

    def test_three_column_index_retained_after_field_alteration(self):
        """
        Test that indexes with 3+ columns are retained when altering one of the fields.
        This ensures the fix works for indexes with more than 2 columns.
        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:
            context_desc = "combined context" if use_single_context else "split contexts"
            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaIdx3Col{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20)),
                                ('b', models.CharField(max_length=20)),
                                ('c', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'b', 'c'], name=f'idx_3col{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='b',
                            field=models.CharField(max_length=50),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_3col',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'b', 'c'},
                    error_msg=(
                        f"Three-column index on ('a', 'b', 'c') was not recreated after field alteration "
                        f"({self._get_context_description(use_single_context)}). Expected index to be restored after ALTER COLUMN operation on middle column."
                    ),
                )

    def test_indexes_retained_for_field_with_db_index_and_meta_indexes(self):
        """
        Test that when a field has indexes from both db_index=True and _meta.indexes, those
        indexes are both retained after altering that field.
        """
        for use_single_context in [False, True]:
            context_desc = "combined context" if use_single_context else "split contexts"
            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaIdxDbIdx{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20, db_index=True)),
                                ('b', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'b'], name=f'idx_dbidx{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='a',
                            field=models.CharField(max_length=40, db_index=True),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_dbidx',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                # Check that _meta_indexes index was recreated
                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'b'},
                    error_msg=(
                        f"Index on ('a', 'b') from _meta.indexes was not recreated after field type change "
                        f"({self._get_context_description(use_single_context)})."
                    ),
                )

                # Check that index from db_index=True was also recreated
                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a'},
                    error_msg=(
                        "Index on 'a' from db_index=True was not recreated "
                        f"after field type change ({self._get_context_description(use_single_context)})."
                    ),
                )

    def test_index_from_meta_indexes_retained_after_type_and_nullability_change(self):
        """
        Test that indexes defined in _meta.indexes are retained when BOTH type and nullability change simultaneously.
        This exercises both code paths in _alter_field (type change AND nullability change).
        The index should only be dropped once and recreated once (tests deduplication logic).
        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaIdxTypeNull{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20)),
                                ('b', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'b'], name=f'idx_typenull{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='a',
                            field=models.CharField(max_length=40, null=True),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_typenull',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'b'},
                    error_msg=(
                        f"Index on ('a', 'b') from _meta.indexes was not recreated after simultaneous type and nullability change "
                        f"({self._get_context_description(use_single_context)}). "
                        f"Expected index to be restored after ALTER COLUMN operation changing both max_length and nullability."
                    ),
                )

    def test_indexes_from_meta_indexes_retained_with_unique_together(self):
        """
        Test that indexes defined in _meta.indexes coexist properly with unique_together constraints.
        Tests the case where a model has overlapping columns participating in both unique_together and
        indexes defined in _meta.indexes. The index defined in _meta.indexes should be retained after field alteration.
        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaIdxUniqTogether{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20)),
                                ('b', models.CharField(max_length=20)),
                                ('c', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AlterUniqueTogether(
                            name=model_name.lower(),
                            unique_together={('a', 'b')},
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'c'], name=f'idx_uniqtog{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='a',
                            field=models.CharField(max_length=40),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_uniqtog',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                # Check that the index (a, c) from _meta.indexes was recreated
                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'c'},
                    error_msg=(
                        f"Index on ('a', 'c') from _meta.indexes was not recreated after field alteration "
                        f"({self._get_context_description(use_single_context)}). "
                        f"Expected index to coexist with unique_together constraint on ('a', 'b')."
                    ),
                )

                # Also verify that unique_together constraint still exists
                unique_constraints = [
                    info for info in result.constraints.values()
                    if info.get('unique') and set(info['columns']) == {'a', 'b'}
                ]
                self.assertTrue(
                    len(unique_constraints) > 0,
                    f"unique_together constraint on ('a', 'b') was lost "
                    f"({self._get_context_description(use_single_context)})."
                )

    def test_index_from_meta_indexes_retained_after_fk_alteration(self):
        """
        Test that indexes defined in _meta.indexes containing ForeignKey fields are retained after FK alteration.
        ForeignKey handling in _alter_field is complex, and this ensures that indexes defined in _meta.indexes
        involving FK fields are properly restored.
        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                ref_model_name = f'TestMetaIdxFKRef{suffix}'
                model_name = f'TestMetaIdxFK{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=ref_model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('name', models.CharField(max_length=50)),
                            ],
                        ),
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('fk_field', models.ForeignKey(
                                    to=f'testapp.{ref_model_name}',
                                    on_delete=models.CASCADE,
                                )),
                                ('other_field', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['fk_field', 'other_field'], name=f'idx_fk{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='fk_field',
                            field=models.ForeignKey(
                                to=f'testapp.{ref_model_name}',
                                on_delete=models.SET_NULL,
                                null=True,
                            ),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_fk',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'fk_field_id', 'other_field'},
                    error_msg=(
                        f"Index on ('fk_field', 'other_field') from _meta.indexes was not recreated after FK alteration "
                        f"({self._get_context_description(use_single_context)}). "
                        f"Expected index to be restored after changing FK from CASCADE to SET_NULL with null=True."
                    ),
                )

    def test_multiple_index_from_meta_indexes_retained(self):
        """
        Test that ALL indexes defined in _meta.indexes are retained when a field participates in multiple indexes.
        A field can be part of multiple different indexes defined in _meta.indexes, and all should be restored
        after altering that field.
        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaMulti{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20)),
                                ('b', models.CharField(max_length=20)),
                                ('c', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'b'], name=f'idx_multi_ab{suffix}'),
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'c'], name=f'idx_multi_ac{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='a',
                            field=models.CharField(max_length=40),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_multi',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                # Check that both indexes defined in _meta.indexes were recreated
                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'b'},
                    error_msg=(
                        f"Index on ('a', 'b') from _meta.indexes was not recreated after field alteration "
                        f"({self._get_context_description(use_single_context)}). "
                        f"Expected BOTH indexes containing field 'a' to be restored."
                    ),
                )

                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'c'},
                    error_msg=(
                        f"Index on ('a', 'c') from _meta.indexes was not recreated after field alteration "
                        f"({self._get_context_description(use_single_context)}). "
                        f"Expected BOTH indexes containing field 'a' to be restored."
                    ),
                )

    def test_index_from_meta_indexes_retained_after_nullability_change_to_not_null(self):
        """
        Test that indexes defined in _meta.indexes are retained when changing field from NULL to NOT NULL.
        This is the reverse direction of the existing nullability test and exercises the
        four-way default alteration path in _alter_field (requires a default value).
        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaIdxNotNull{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20, null=True)),
                                ('b', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'b'], name=f'idx_notnull{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='a',
                            field=models.CharField(max_length=20, null=False, default=''),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_notnull',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'b'},
                    error_msg=(
                        f"Index on ('a', 'b') from _meta.indexes was not recreated after nullability change from NULL to NOT NULL "
                        f"({self._get_context_description(use_single_context)}). "
                        f"Expected index to be restored after ALTER COLUMN operation with default value handling."
                    ),
                )

    @expectedFailure
    def test_autofield_type_change_preserves_indexes(self):
        """
        Test that indexes defined in _meta.indexes are retained when changing AutoField to BigAutoField.
        This exercises the special AutoField/BigAutoField restoration path in _alter_field
        which restores ALL indexes on ALL fields, not just the altered field.
        Runs with both split and combined migration contexts.

        KNOWN BUG: This test currently fails because the AutoField/BigAutoField special
        handling block only restores indexes defined via db_index=True and then breaks
        out of the loop, skipping the subsequent code that restores indexes defined in _meta.indexes.
        The fix would require the AutoField block to also iterate through Meta.indexes
        or to not break early, allowing the subsequent restoration code to run.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaIdxAutoField{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20)),
                                ('b', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['a', 'b'], name=f'idx_auto{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='id',
                            field=models.BigAutoField(primary_key=True),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_auto',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'b'},
                    error_msg=(
                        f"Index on ('a', 'b') from _meta.indexes was not recreated after AutoField to BigAutoField change "
                        f"({self._get_context_description(use_single_context)}). "
                        f"Expected index to be restored via AutoField/BigAutoField special restoration path."
                    ),
                )

    def test_pk_type_change_preserves_indexes(self):
        """
        Test that indexes defined in _meta.indexes are retained when changing primary key type.
        This tests the primary key restoration path alongside the restoration of indexes from _meta.indexes.
        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestMetaIdxPK{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20)),
                                ('b', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AddIndex(
                            model_name=model_name.lower(),
                            index=models.Index(fields=['id', 'a'], name=f'idx_pk{suffix}'),
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='id',
                            field=models.BigAutoField(primary_key=True),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_mc_pk',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                # Verify primary key still exists
                pk_constraints = [
                    info for info in result.constraints.values()
                    if info.get('primary_key')
                ]
                self.assertTrue(
                    len(pk_constraints) > 0,
                    f"Primary key was not restored ({self._get_context_description(use_single_context)})."
                )

                # Verify index from _meta.indexes including PK column was restored
                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'id', 'a'},
                    error_msg=(
                        f"Index on ('id', 'a') from _meta.indexes was not recreated after PK type change "
                        f"({self._get_context_description(use_single_context)}). "
                        f"Expected index containing PK column to be restored."
                    ),
                )

    @skipIf(VERSION >= (5, 1), "index_together removed in Django 5.1")
    def test_index_together_retained_after_type_change(self):
        """
        Test that index_together indexes are retained when altering a field type.

        IMPORTANT: This test documents the known limitation that index_together is only
        restored when the field does NOT have db_index=True. If a field has both
        db_index=True AND is in index_together, only the index from db_index=True is restored
        through the standard restoration path. This is intentional behavior for the
        deprecated index_together API (removed in Django 5.1+).

        This test uses a field WITHOUT db_index=True to verify the index_together
        restoration works in that scenario.

        Runs with both split and combined migration contexts.
        """
        for use_single_context in [False, True]:

            with self.subTest(single_context=use_single_context):
                suffix = '_combined' if use_single_context else '_split'
                model_name = f'TestIdxTogether{suffix}'

                class TestMigrationA(migrations.Migration):
                    initial = True

                    operations = [
                        migrations.CreateModel(
                            name=model_name,
                            fields=[
                                ('id', models.AutoField(primary_key=True)),
                                ('a', models.CharField(max_length=20)),  # No db_index=True
                                ('b', models.CharField(max_length=20)),
                            ],
                        ),
                        migrations.AlterIndexTogether(
                            name=model_name.lower(),
                            index_together={('a', 'b')},
                        ),
                    ]

                class TestMigrationB(migrations.Migration):
                    operations = [
                        migrations.AlterField(
                            model_name=model_name.lower(),
                            name='a',
                            field=models.CharField(max_length=40),
                        ),
                    ]

                result = self._run_migration_test(
                    MigrationA=TestMigrationA,
                    MigrationB=TestMigrationB,
                    migration_name_prefix='test_idxtog',
                    model_name=model_name,
                    use_single_context=use_single_context,
                )

                # Verify index_together index was restored
                self._assert_index_exists(
                    result.constraints,
                    expected_columns={'a', 'b'},
                    error_msg=(
                        f"index_together index on ('a', 'b') was not recreated after type change "
                        f"({self._get_context_description(use_single_context)}). "
                        f"Expected index_together to be restored for field without db_index=True."
                    ),
                )





class TestAddAndAlterUniqueIndex(TestCase):

    def test_alter_unique_nullable_to_non_nullable(self):
        """
        Test a single migration that creates a field with unique=True and null=True and then alters
        the field to set null=False. See https://github.com/microsoft/mssql-django/issues/22
        """
        operations = [
            migrations.CreateModel(
                "TestAlterNullableInUniqueField",
                [
                    ("id", models.AutoField(primary_key=True)),
                    ("a", models.CharField(max_length=4, unique=True, null=True)),
                ]
            ),
            migrations.AlterField(
                "testalternullableinuniquefield",
                "a",
                models.CharField(max_length=4, unique=True)
            )
        ]

        project_state = ProjectState()
        new_state = project_state.clone()
        migration = Migration("name", "testapp")
        migration.operations = operations

        try:
            with connection.schema_editor(atomic=True) as editor:
                migration.apply(new_state, editor)
        except django.db.utils.ProgrammingError as e:
            self.fail('Check if can alter field from unique, nullable to unique non-nullable for issue #23, AlterField failed with exception: %s' % e)

class TestKeepIndexWithDbcomment(TestCase):
    def _find_key_with_type_idx(self, input_dict):
        for key, value in input_dict.items():
            if value.get("type") == "idx":
                return key
        return None

    @skipIf(VERSION < (4, 2), "db_comment not available before 4.2")
    def test_drop_foreignkey(self):
        app_label = "test_drop_foreignkey"
        operations = [
                migrations.CreateModel(
                    name="brand",
                    fields=[
                        ("id", models.AutoField(primary_key=True)),
                        ("name", models.CharField(max_length=100)),
                    ],
                ),
                migrations.CreateModel(
                    name="car1",
                    fields=[
                        ("id", models.AutoField(primary_key=True)),
                        (
                            "brand",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                to="test_drop_foreignkey.brand",
                                related_name="car1",
                                db_constraint=True,
                            ),
                        ),
                    ],
                ),
                migrations.CreateModel(
                    name="car2",
                    fields=[
                        ("id", models.AutoField(primary_key=True)),
                        (
                            "brand",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                to="test_drop_foreignkey.brand",
                                related_name="car2",
                                db_constraint=True,
                            ),
                        ),
                    ],
                ),
                migrations.CreateModel(
                    name="car3",
                    fields=[
                        ("id", models.AutoField(primary_key=True)),
                        (
                            "brand",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                to="test_drop_foreignkey.brand",
                                related_name="car3",
                                db_constraint=True,
                            ),
                        ),
                    ],
                ),
            ]
        migration = Migration("name", app_label)
        migration.operations = operations
        with connection.schema_editor(atomic=True) as editor:
            project_state = migration.apply(ProjectState(), editor)

        alter_fk_car1 = migrations.AlterField(
            model_name="car1",
            name="brand",
            field=models.ForeignKey(
                to="test_drop_foreignkey.brand",
                on_delete=django.db.models.deletion.CASCADE,
                db_constraint=False,
                related_name="car1",
            ),
        )
        alter_fk_car2 = migrations.AlterField(
            model_name="car2",
            name="brand",
            field=models.ForeignKey(
                to="test_drop_foreignkey.brand",
                on_delete=django.db.models.deletion.CASCADE,
                db_constraint=False,
                related_name="car2",
                db_comment=""
            ),
        )
        alter_fk_car3 = migrations.AlterField(
            model_name="car3",
            name="brand",
            field=models.ForeignKey(
                to="test_drop_foreignkey.brand",
                on_delete=django.db.models.deletion.CASCADE,
                db_constraint=False,
                related_name="car3",
                db_comment="fk_on_delete_keep_index"
            ),
        )
        new_state = project_state.clone()
        with connection.schema_editor(atomic=True) as editor:
            alter_fk_car1.state_forwards("test_drop_foreignkey", new_state)
            alter_fk_car1.database_forwards(
                "test_drop_foreignkey", editor, project_state, new_state
            )
        car_index = self._find_key_with_type_idx(
            get_constraints(
                table_name=new_state.apps.get_model(
                    "test_drop_foreignkey", "car1"
                )._meta.db_table
            )
        )
        # Test alter foreignkey without db_comment field
        # The index should be dropped (keep the old behavior)
        self.assertIsNone(car_index)

        project_state = new_state
        new_state = new_state.clone()
        with connection.schema_editor(atomic=True) as editor:
            alter_fk_car2.state_forwards("test_drop_foreignkey", new_state)
            alter_fk_car2.database_forwards(
                "test_drop_foreignkey", editor, project_state, new_state
            )
        car_index = self._find_key_with_type_idx(
            get_constraints(
                table_name=new_state.apps.get_model(
                    "test_drop_foreignkey", "car2"
                )._meta.db_table
            )
        )
        # Test alter fk with empty db_comment
        self.assertIsNone(car_index)

        project_state = new_state
        new_state = new_state.clone()
        with connection.schema_editor(atomic=True) as editor:
            alter_fk_car3.state_forwards("test_drop_foreignkey", new_state)
            alter_fk_car3.database_forwards(
                "test_drop_foreignkey", editor, project_state, new_state
            )
        car_index = self._find_key_with_type_idx(
            get_constraints(
                table_name=new_state.apps.get_model(
                    "test_drop_foreignkey", "car3"
                )._meta.db_table
            )
        )
        # Test alter fk with fk_on_delete_keep_index in db_comment
        # Index should be preserved in this case
        self.assertIsNotNone(car_index)
