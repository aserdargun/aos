"""Read-only structural preview for one private synthetic skill variation plan."""

import argparse
import json
import os
from pathlib import Path
import stat

from .contracts import REPO_ROOT, canonical, digest
from .lifecycle import private_directory
from .site_knowledge import SiteKnowledgeStore
from .site_skill import SiteSkillStore
from .site_skill_validation import SiteSkillValidationPlan, preview_skill_validation
from .web_application import WebApplicationProfiles


MAX_PLAN_BYTES = 16384
ERROR = 'Site skill validation preview failed'


class PrivateArgumentParser(argparse.ArgumentParser):
    def __init__(self, *arguments, **options):
        options.setdefault('allow_abbrev', False)
        super().__init__(*arguments, **options)

    def error(self, message):
        raise SystemExit(ERROR)


def read_plan_source(path: Path) -> SiteSkillValidationPlan:
    directory = private_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > MAX_PLAN_BYTES):
                raise ValueError('invalid_private_validation_plan')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                payload = stream.read(MAX_PLAN_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink',
                      'st_mode', 'st_uid')
            if (len(payload) > MAX_PLAN_BYTES or len(payload) != before.st_size
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))):
                raise ValueError('validation_plan_changed')

            def unique_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate_validation_plan_key')
                    result[key] = value
                return result

            value = json.loads(payload, object_pairs_hook=unique_pairs)
            plan = SiteSkillValidationPlan.model_validate(value)
            if payload != canonical(plan.model_dump()).encode():
                raise ValueError('validation_plan_not_canonical')
            return plan
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def main():
    parser = PrivateArgumentParser(description='Read-only synthetic skill structure preview')
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--pages', type=Path, default=REPO_ROOT / 'data/site-knowledge')
    parser.add_argument('--store', type=Path, default=REPO_ROOT / 'data/site-skills')
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--skill-sha256', required=True)
    parser.add_argument('--plan-sha256', required=True)
    arguments, unknown = parser.parse_known_args()
    if unknown:
        raise SystemExit(ERROR)
    try:
        plan = read_plan_source(arguments.plan)
        if (plan.skill_sha256 != arguments.skill_sha256
                or digest(plan.model_dump()) != arguments.plan_sha256):
            raise ValueError('validation_plan_selection_mismatch')
        profiles = WebApplicationProfiles(arguments.profiles)
        pages = SiteKnowledgeStore(arguments.pages, profiles)
        report = preview_skill_validation(SiteSkillStore(arguments.store, profiles, pages), plan)
    except (OSError, ValueError):
        raise SystemExit(ERROR) from None
    print(canonical(report))


if __name__ == '__main__':
    main()
