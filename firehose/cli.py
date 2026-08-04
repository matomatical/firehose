import tyro

import firehose.harvest
import firehose.classes
import firehose.index
import firehose.sample
import firehose.server
import firehose.vis


def cli():
    tyro.extras.subcommand_cli_from_dict({
        # full-metadata mirror (all categories) and its derived index
        'mirror': firehose.harvest.mirror,
        'rebuild-index': firehose.index.rebuild_index,
        # serve this machine's data to remote firehose clients
        'serve': firehose.server.serve,
        # report the store's state (mirror, harvests, event log)
        'status': firehose.vis.status,
        # sample
        'sample': firehose.sample.sample,
        # print arXiv category catalog
        'classes': firehose.classes.classes,
        # visualising readlog and cache
        'unread': firehose.vis.unread,
        'calendar': firehose.vis.reading_calendar,
        'linear': firehose.vis.linear,
        'hilbert': firehose.vis.hilbert,
        'days': firehose.vis.all_submitted_dates,
        'months': firehose.vis.all_submitted_months,
        'years': firehose.vis.all_submitted_years,
        # scanning-time analytics from the scan log
        'time': firehose.vis.scan_time,
    })
