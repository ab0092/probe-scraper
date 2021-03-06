# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from collections import defaultdict
from dateutil.tz import tzlocal
import argparse
import datetime
import errno
import json
import os
import tempfile
import traceback

from emailer import send_ses
from parsers.events import EventsParser
from parsers.histograms import HistogramsParser
from parsers.scalars import ScalarsParser
from parsers.repositories import RepositoriesParser
from scrapers import git_scraper, moz_central_scraper
from schema import And, Optional, Schema
import transform_probes
import transform_revisions


class DummyParser:
    def parse(self, files):
        return {}


FROM_EMAIL = "telemetry-alerts@mozilla.com"
DEFAULT_TO_EMAIL = "dev-telemetry-alerts@mozilla.com"


PARSERS = {
    # This lists the available probe registry parsers:
    # parser type -> parser
    'histogram': HistogramsParser(),
    'scalar': ScalarsParser(),
    'event': EventsParser(),
}


def general_data():
    return {
        "lastUpdate": datetime.datetime.now(tzlocal()).isoformat(),
    }


def dump_json(data, out_dir, file_name):
    # Make sure that the output directory exists. This also creates
    # intermediate directories if needed.
    try:
        os.makedirs(out_dir)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    path = os.path.join(out_dir, file_name)
    with open(path, 'w') as f:
        print "  " + path
        json.dump(data, f, sort_keys=True, indent=2)


def write_moz_central_probe_data(probe_data, revisions, out_dir):
    # Save all our files to "outdir/firefox/..." to mimic a REST API.
    base_dir = os.path.join(out_dir, "firefox")

    print "\nwriting output:"
    dump_json(general_data(), base_dir, "general")
    dump_json(revisions, base_dir, "revisions")

    # Break down the output by channel. We don't need to write a revisions
    # file in this case, the probe data will contain human readable version
    # numbers along with revision numbers.
    for channel, channel_probes in probe_data.iteritems():
        data_dir = os.path.join(base_dir, channel, "main")
        dump_json(channel_probes, data_dir, "all_probes")


def write_external_probe_data(repo_data, out_dir):
    # Save all our files to "outdir/<repo>/..." to mimic a REST API.
    for repo, probe_data in repo_data.iteritems():
        base_dir = os.path.join(out_dir, repo)

        print "\nwriting output:"
        dump_json(general_data(), base_dir, "general")

        data_dir = os.path.join(base_dir, "mobile-metrics")
        dump_json(probe_data, data_dir, "all_probes")


def write_repositories_data(repos, out_dir):
    json_data = [r.to_dict() for r in repos]
    dump_json(json_data, os.path.join(out_dir, "mobile-metrics"), "repositories")


def load_moz_central_probes(cache_dir, out_dir):
    # Scrape probe data from repositories.
    node_data = moz_central_scraper.scrape(cache_dir)

    # Parse probe data from files into the form:
    # channel_name -> {
    #   node_id -> {
    #     histogram: {
    #       name: ...,
    #       ...
    #     },
    #     scalar: {
    #       ...
    #     },
    #   },
    #   ...
    # }
    probes = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for channel, nodes in node_data.iteritems():
        for node_id, details in nodes.iteritems():
            for probe_type, paths in details['registries'].iteritems():
                results = PARSERS[probe_type].parse(paths, details["version"])
                probes[channel][node_id][probe_type] = results

    # Transform extracted data: get both the monolithic and by channel probe data.
    revisions = transform_revisions.transform(node_data)
    probes_by_channel = transform_probes.transform(probes, node_data,
                                                   break_by_channel=True)
    probes_by_channel["all"] = transform_probes.transform(probes, node_data,
                                                          break_by_channel=False)

    # Serialize the probe data to disk.
    write_moz_central_probe_data(probes_by_channel, revisions, out_dir)


def check_git_probe_structure(data):
    schema = Schema({
        str: {
            And(str, lambda x: len(x) == 40): {
                Optional("histogram"): [And(str, lambda x: os.path.exists(x))],
                Optional("event"): [And(str, lambda x: os.path.exists(x))],
                Optional("scalar"): [And(str, lambda x: os.path.exists(x))]
            }
        }
    })

    schema.validate(data)


def load_git_probes(cache_dir, out_dir, repositories_file, dry_run):
    repositories = RepositoriesParser().parse(repositories_file)
    commit_timestamps, repos_probes_data, emails = git_scraper.scrape(cache_dir, repositories)

    check_git_probe_structure(repos_probes_data)

    # Parse probe data from files into the form:
    # <repo_name> -> {
    #   <commit-hash> -> {
    #     "histogram": {
    #       <histogram_name>: {
    #         ...
    #       },
    #       ...
    #     },
    #     "scalar": {
    #       ...
    #     },
    #   },
    #   ...
    # }
    probes = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for repo_name, commits in repos_probes_data.iteritems():
        for commit_hash, probe_types in commits.iteritems():
            for probe_type, paths in probe_types.iteritems():
                try:
                    results = PARSERS[probe_type].parse(paths)
                    probes[repo_name][commit_hash][probe_type] = results
                except Exception:
                    msg = "Improper file in {}\n{}".format(', '.join(paths), traceback.format_exc())
                    emails[repo_name]["emails"].append({
                        "subject": "Probe Scraper: Improper File",
                        "message": msg
                    })

    probes_by_repo = transform_probes.transform_by_hash(commit_timestamps, probes)

    write_external_probe_data(probes_by_repo, out_dir)

    write_repositories_data(repositories, out_dir)

    for repo_name, email_info in emails.items():
        addresses = email_info["addresses"] + [DEFAULT_TO_EMAIL]
        for email in email_info["emails"]:
            send_ses(FROM_EMAIL, email["subject"], email["message"], addresses, dryrun=dry_run)


def main(cache_dir,
         out_dir,
         process_moz_central_probes,
         process_git_probes,
         repositories_file,
         dry_run):

    process_both = not (process_moz_central_probes or process_git_probes)
    if process_moz_central_probes or process_both:
        load_moz_central_probes(cache_dir, out_dir)
    if process_git_probes or process_both:
        load_git_probes(cache_dir, out_dir, repositories_file, dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--cache-dir',
                        help='Cache directory. If empty, will be filled with the probe files.',
                        action='store',
                        default=tempfile.mkdtemp())
    parser.add_argument('--out-dir',
                        help='Directory to store output files in.',
                        action='store',
                        default='.')
    parser.add_argument('--repositories-file',
                        help='Repositories YAML file location.',
                        action='store',
                        default='repositories.yaml')
    parser.add_argument('--dry-run',
                        help='Whether emails should be sent.',
                        action='store_true')

    group = parser.add_mutually_exclusive_group()
    group.add_argument('--only-moz-central-probes',
                       help='Only scrape moz-central probes',
                       action='store_true')
    group.add_argument('--only-git-probes',
                       help='Only scrape probes in remote git repos',
                       action='store_true')

    args = parser.parse_args()
    main(args.cache_dir,
         args.out_dir,
         args.only_moz_central_probes,
         args.only_git_probes,
         args.repositories_file,
         args.dry_run)
