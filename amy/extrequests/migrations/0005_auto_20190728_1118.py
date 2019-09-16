# Generated by Django 2.1.7 on 2019-07-28 11:18

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('extrequests', '0004_auto_20190725_1907'),
    ]

    operations = [
        migrations.AlterField(
            model_name='selforganizedsubmission',
            name='institution',
            field=models.ForeignKey(blank=True, help_text="If your institution isn't on the list, enter its name below the list.", null=True, on_delete=django.db.models.deletion.PROTECT, to='workshops.Organization', verbose_name='Institutional affiliation'),
        ),
        migrations.AlterField(
            model_name='selforganizedsubmission',
            name='workshop_types_other_explain',
            field=models.TextField(blank=True, help_text='For example "We are teaching Software Carpentry\'s Git lesson only" or "We are teaching Data Carpentry\'s Ecology workshop, but not teaching a programming language."', verbose_name='If you selected "Mix & Match", please provide more information here'),
        ),
        migrations.AlterField(
            model_name='selforganizedsubmission',
            name='workshop_url',
            field=models.URLField(blank=True, default='', help_text='Use the link to the website, not the repository. This is typically in the format <a>https://username.github.io/YYYY-MM-DD-sitename</a>.', max_length=255, verbose_name='Please share your workshop URL'),
        ),
        migrations.AlterField(
            model_name='workshopinquiryrequest',
            name='institution',
            field=models.ForeignKey(blank=True, help_text="If your institution isn't on the list, enter its name below the list.", null=True, on_delete=django.db.models.deletion.PROTECT, to='workshops.Organization', verbose_name='Institutional affiliation'),
        ),
        migrations.AlterField(
            model_name='workshopinquiryrequest',
            name='preferred_dates',
            field=models.DateField(help_text='Our workshops typically run two full days. Please select your preferred first day for the workshop. If you do not have exact dates or are interested in an alternative schedule, please indicate so below. Because we need to coordinate with instructors, a minimum of 2-3 months lead time is required for workshop planning.', verbose_name='Preferred dates'),
        ),
    ]