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
            name='RegularChild',
            fields=[
                ('id', models.AutoField(primary_key=True)),
                ('parent', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to='regular_fk_pk_widening_migration.parent',
                )),
                ('payload', models.CharField(default='x', max_length=20)),
            ],
        ),
        migrations.AlterField(
            model_name='parent',
            name='id',
            field=models.BigAutoField(primary_key=True),
        ),
    ]
