from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('pk_widening_migration', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='parent',
            name='id',
            field=models.BigAutoField(primary_key=True),
        ),
    ]
