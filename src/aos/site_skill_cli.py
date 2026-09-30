"""Metadata-only CLI for private, non-executable site skill drafts."""

import argparse
import json
import os
from pathlib import Path
import re
import stat

from .contracts import REPO_ROOT, canonical
from .lifecycle import private_directory
from .site_knowledge import SiteKnowledgeStore
from .site_skill import MAX_SKILL_BYTES, SiteSkillDraft, SiteSkillStore
from .web_application import WebApplicationProfiles


class PrivateArgumentParser(argparse.ArgumentParser):
    def __init__(self, *arguments, **options):
        options.setdefault('allow_abbrev', False)
        super().__init__(*arguments, **options)

    def error(self, message):
        raise SystemExit('Site skill draft operation failed')


def read_skill_source(path: Path) -> SiteSkillDraft:
    directory = private_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > MAX_SKILL_BYTES):
                raise ValueError('invalid_private_skill_source')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                payload = stream.read(MAX_SKILL_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_mode', 'st_uid')
            if (len(payload) > MAX_SKILL_BYTES or len(payload) != before.st_size
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))):
                raise ValueError('skill_source_changed')

            def unique_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate_skill_source_key')
                    result[key] = value
                return result

            return SiteSkillDraft.model_validate(json.loads(payload, object_pairs_hook=unique_pairs))
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def main():
    parser = PrivateArgumentParser(description='Private symbolic site skill drafts')
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--pages', type=Path, default=REPO_ROOT / 'data/site-knowledge')
    parser.add_argument('--store', type=Path, default=REPO_ROOT / 'data/site-skills')
    commands = parser.add_subparsers(dest='command', required=True)
    preview = commands.add_parser('preview')
    preview.add_argument('--skill', type=Path, required=True)
    register = commands.add_parser('register')
    register.add_argument('--skill', type=Path, required=True)
    register.add_argument('--confirm-sha256', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--sha256', required=True)
    inspect.add_argument('--selected-profile-sha256', required=True)
    inspect.add_argument('--selected-page-sha256', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--profile-sha256', required=True)
    listing.add_argument('--model-role', choices=('system1', 'system2'), required=True)
    arguments, unknown = parser.parse_known_args()
    if unknown:
        raise SystemExit('Site skill draft operation failed')
    profiles = WebApplicationProfiles(arguments.profiles)
    store = SiteSkillStore(arguments.store, profiles, SiteKnowledgeStore(arguments.pages, profiles))
    try:
        if arguments.command in {'preview', 'register'}:
            skill = read_skill_source(arguments.skill)
            report = store.preview(skill)
            if arguments.command == 'register':
                store.register(skill, confirm_sha256=arguments.confirm_sha256)
        elif arguments.command == 'inspect':
            if (re.fullmatch('[a-f0-9]{64}', arguments.selected_profile_sha256) is None
                    or re.fullmatch('[a-f0-9]{64}', arguments.selected_page_sha256) is None):
                raise ValueError('invalid_skill_inspection_scope')
            skill = store.get(arguments.sha256)
            report = store.preview(skill)
            report['status'] = ('draft_match' if
                skill.profile_sha256 == arguments.selected_profile_sha256
                and skill.page_draft_sha256 == arguments.selected_page_sha256 else 'stale')
        else:
            report = {'schema_version': '1.0', 'mode': 'draft_metadata_list',
                      'skills': store.list(profile_sha256=arguments.profile_sha256,
                                           model_role=arguments.model_role)}
    except (OSError, ValueError):
        raise SystemExit('Site skill draft operation failed') from None
    print(canonical(report))


if __name__ == '__main__':
    main()
