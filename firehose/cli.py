import tyro

import firehose.harvest
import firehose.classes
import firehose.sample
import firehose.vis_readlog
import firehose.vis_cache


def cli():
    tyro.extras.subcommand_cli_from_dict({
        # harvest
        'harvest': firehose.harvest.harvest,
        # check classes
        'classes': firehose.classes.classes,
        # sample
        'sample': firehose.sample.sample,
        'nsample': firehose.sample.nsample,
        # visualising readlog
        'calendar': firehose.vis_readlog.calendar,
        'linear': firehose.vis_readlog.linear,
        'hilbert': firehose.vis_readlog.hilbert,
        # visualising cache
        'days': firehose.vis_cache.all_submitted_dates,
        'months': firehose.vis_cache.all_submitted_months,
        'years': firehose.vis_cache.all_submitted_years,
    })
