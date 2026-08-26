from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Parent',
            fields=[
                ('id', models.AutoField(primary_key=True)),
                ('name', models.CharField(max_length=20)),
            ],
        ),
        migrations.CreateModel(
            name='PlainChild',
            fields=[
                ('id', models.AutoField(primary_key=True)),
                ('parent', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    to='pk_widening_migration.parent',
                )),
                ('payload', models.CharField(default='x', max_length=20)),
            ],
        ),
        migrations.CreateModel(
            name='ConstrainedChild',
            fields=[
                ('id', models.AutoField(primary_key=True)),
                ('parent', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    to='pk_widening_migration.parent',
                )),
            ],
            options={
                'constraints': [
                    models.UniqueConstraint(
                        fields=('parent',), name='pk_widening_constrained_child_parent_uniq',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='SharedChild',
            fields=[
                ('parent', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    primary_key=True,
                    serialize=False,
                    to='pk_widening_migration.parent',
                )),
                ('payload', models.CharField(default='x', max_length=20)),
            ],
        ),
    ]
