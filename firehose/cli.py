import tyro

import firehose.harvest
import firehose.classes
import firehose.sample
import firehose.vis


def cli():
    tyro.extras.subcommand_cli_from_dict({
        # harvest
        'harvest': firehose.harvest.harvest,
        # sample
        'sample': firehose.sample.sample,
        # print arXiv category catalog
        'classes': firehose.classes.classes,
        # visualising readlog and cache
        'calendar': firehose.vis.reading_calendar,
        'linear': firehose.vis.linear,
        'hilbert': firehose.vis.hilbert,
        'days': firehose.vis.all_submitted_dates,
        'months': firehose.vis.all_submitted_months,
        'years': firehose.vis.all_submitted_years,
        # scanning-time analytics from the scan log
        'time': firehose.vis.scan_time,
    })
