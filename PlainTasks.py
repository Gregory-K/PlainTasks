#!/usr/bin/python
# -*- coding: utf-8 -*-

import sublime, sublime_plugin
import os
import re
import webbrowser
import itertools
import threading
from datetime import datetime, timezone, timedelta
import time
import io

from .APlainTasksCommon import PlainTasksBase, PlainTasksFold, get_all_projects_and_separators

platform = sublime.platform()
NT = platform == 'windows'

if NT:
    import contextlib
    import subprocess

    @contextlib.contextmanager
    def create_process(*args, **kwargs):
        proc = subprocess.Popen(*args, **kwargs)
        try:
            yield proc
        finally:
            proc.terminate()


def tznow():
    t = time.time()
    d = datetime.fromtimestamp(t)
    u = datetime.utcfromtimestamp(t)
    return d.replace(tzinfo=timezone(d - u))


def check_parentheses(date_format, regex_group, is_date=False):
    if is_date:
        try:
            parentheses = regex_group if datetime.strptime(regex_group.strip(), date_format) else ''
        except ValueError:
            parentheses = ''
    else:
        try:
            parentheses = '' if datetime.strptime(regex_group.strip(), date_format) else regex_group
        except ValueError:
            parentheses = regex_group
    return parentheses


def format_delta(view, delta):
    if view.settings().get('decimal_minutes', False):
        days = delta.days
        delta = f'{days or ""}{" day, " if days == 1 else ""}{" days, " if days > 1 else ""}{f"{delta.seconds / 3600.0:.2f}" if delta.seconds else ""}'
    else:
        delta = str(delta)
    if delta[~7:] == ' 0:00:00' or delta == '0:00:00':  # strip meaningless time
        delta = delta[:~6]
    elif delta[~2:] == ':00':  # strip meaningless seconds
        delta = delta[:~2]
    return delta.strip(' ,')


class PlainTasksNewCommand(PlainTasksBase):
    def runCommand(self, edit):
        # list for ST3 support;
        # reversed because with multiple selections regions would be messed up after first iteration
        regions = itertools.chain(*(reversed(self.view.lines(region)) for region in reversed(list(self.view.sel()))))
        header_to_task = self.view.settings().get('header_to_task', False)
        # ST3 (3080) moves sel when call view.replace only by delta between original and
        # new regions, so if sel is not in eol and we replace line with two lines,
        # then cursor won’t be on next line as it should
        sels = self.view.sel()
        eol  = None
        for i, line in enumerate(regions):
            line_contents  = self.view.substr(line).rstrip()
            not_empty_line = re.match('^(\s*)(\S.*)$', self.view.substr(line))
            empty_line     = re.match('^(\s+)$', self.view.substr(line))
            current_scope  = self.view.scope_name(line.a)
            eol = line.b  # need for ST3 when new content has line break
            if 'item' in current_scope:
                grps = not_empty_line.groups()
                line_contents = self.view.substr(line) + '\n' + grps[0] + self.open_tasks_bullet + self.tasks_bullet_space
            elif 'header' in current_scope and line_contents and not header_to_task:
                grps = not_empty_line.groups()
                line_contents = self.view.substr(line) + '\n' + grps[0] + self.before_tasks_bullet_spaces + self.open_tasks_bullet + self.tasks_bullet_space
            elif 'separator' in current_scope:
                grps = not_empty_line.groups()
                line_contents = self.view.substr(line) + '\n' + grps[0] + self.before_tasks_bullet_spaces + self.open_tasks_bullet + self.tasks_bullet_space
            elif ('header' and 'separator') not in current_scope or header_to_task:
                eol = None
                if not_empty_line:
                    grps = not_empty_line.groups()
                    line_contents = (grps[0] if len(grps[0]) > 0 else self.before_tasks_bullet_spaces) + self.open_tasks_bullet + self.tasks_bullet_space + grps[1]
                elif empty_line:  # only whitespaces
                    grps = empty_line.groups()
                    line_contents = grps[0] + self.open_tasks_bullet + self.tasks_bullet_space
                else:  # completely empty, no whitespaces
                    line_contents = self.before_tasks_bullet_spaces + self.open_tasks_bullet + self.tasks_bullet_space
            else:
                print('oops, need to improve PlainTasksNewCommand')
            if eol:
                # move cursor to eol of original line, workaround for ST3
                sels.subtract(sels[~i])
                sels.add(sublime.Region(eol, eol))
            self.view.replace(edit, line, line_contents)

        # convert each selection to single cursor, ready to type
        new_selections = []
        for sel in list(self.view.sel()):
            eol = self.view.line(sel).b
            new_selections.append(sublime.Region(eol, eol))
        self.view.sel().clear()
        for sel in new_selections:
            self.view.sel().add(sel)

        PlainTasksStatsStatus.set_stats(self.view)
        self.view.run_command('plain_tasks_toggle_highlight_past_due')


class PlainTasksNewWithDateCommand(PlainTasksBase):
    def runCommand(self, edit):
        self.view.run_command('plain_tasks_new')
        sels = list(self.view.sel())
        suffix = ' @created%s' % tznow().strftime(self.date_format)
        points = []
        for s in reversed(sels):
            if self.view.substr(sublime.Region(s.b - 2, s.b)) == '  ':
                point = s.b - 2  # keep double whitespace at eol
            else:
                point = s.b
            self.view.insert(edit, point, suffix)
            points.append(point)
        self.view.sel().clear()
        offset = len(suffix)
        for i, sel in enumerate(sels):
            self.view.sel().add(sublime.Region(points[~i] + i*offset, points[~i] + i*offset))


class PlainTasksCompleteCommand(PlainTasksBase):
    def runCommand(self, edit):
        original = [r for r in self.view.sel()]
        done_line_end, now = self.format_line_end(self.done_tag, tznow())
        offset = len(done_line_end)
        rom = r'^(\s*)(\[\s\]|.)(\s*.*)$'
        rdm = r'''
            (?x)^(\s*)(\[x\]|.)                           # 0,1 indent & bullet
            (\s*[^\b]*?(?:[^\@]|(?<!\s)\@|\@(?=\s))*?\s*) #   2 very task
            (?=
              ((?:\s@done|@project|@[wl]asted|$).*)   # 3 ending either w/ done or w/o it & no date
              |                                       #   or
              (?:[ \t](\([^()]*\))\s*([^@]*|(?:@project|@[wl]asted).*))?$ # 4 date & possible project tag after
            )
            '''  # rcm is the same, except bullet & ending
        rcm = r'^(\s*)(\[\-\]|.)(\s*[^\b]*?(?:[^\@]|(?<!\s)\@|\@(?=\s))*?\s*)(?=((?:\s@cancelled|@project|@[wl]asted|$).*)|(?:[ \t](\([^()]*\))\s*([^@]*|(?:@project|@[wl]asted).*))?$)'
        started = r'^\s*[^\b]*?\s*@started(\([\d\w,\.:\-\/ @]*\)).*$'
        toggle = r'@toggle(\([\d\w,\.:\-\/ @]*\))'

        regions = itertools.chain(*(reversed(self.view.lines(region)) for region in reversed(list(self.view.sel()))))
        for line in regions:
            line_contents = self.view.substr(line)
            open_matches = re.match(rom, line_contents, re.U)
            done_matches = re.match(rdm, line_contents, re.U)
            canc_matches = re.match(rcm, line_contents, re.U)
            started_matches = re.findall(started, line_contents, re.U)
            toggle_matches = re.findall(toggle, line_contents, re.U)

            done_line_end = done_line_end.rstrip()
            if line_contents.endswith('  '):
                done_line_end += '  '  # keep double whitespace at eol
                dblspc = '  '
            else:
                dblspc = ''

            current_scope = self.view.scope_name(line.a)
            if 'pending' in current_scope:
                grps = open_matches.groups()
                len_dle = self.view.insert(edit, line.end(), done_line_end)
                replacement = f'{grps[0]}{self.done_tasks_bullet}{grps[2].rstrip()}'
                self.view.replace(edit, line, replacement)
                self.view.run_command(
                    'plain_tasks_calculate_time_for_task', {
                        'started_matches': started_matches,
                        'toggle_matches': toggle_matches,
                        'now': now,
                        'eol': line.a + len(replacement) + len_dle}
                )
            elif 'header' in current_scope:
                eol = self.view.insert(edit, line.end(), done_line_end)
                self.view.run_command(
                    'plain_tasks_calculate_time_for_task', {
                        'started_matches': started_matches,
                        'toggle_matches': toggle_matches,
                        'now': now,
                        'eol': line.end() + eol}
                )
                indent = re.match('^(\s*)\S', line_contents, re.U)
                self.view.insert(edit, line.begin() + len(indent.group(1)), f'{self.done_tasks_bullet} ')
                self.view.run_command('plain_tasks_calculate_total_time_for_project', {'start': line.a})
            elif 'completed' in current_scope:
                grps = done_matches.groups()
                parentheses = check_parentheses(self.date_format, grps[4] or '')
                replacement = f'{grps[0]}{self.open_tasks_bullet}{grps[2]}{parentheses}'
                self.view.replace(edit, line, replacement.rstrip() + dblspc)
                offset = -offset
            elif 'cancelled' in current_scope:
                grps = canc_matches.groups()
                len_dle = self.view.insert(edit, line.end(), done_line_end)
                parentheses = check_parentheses(self.date_format, grps[4] or '')
                replacement = f'{grps[0]}{self.done_tasks_bullet}{grps[2]}{parentheses}'
                self.view.replace(edit, line, replacement.rstrip())
                offset = -offset
                self.view.run_command(
                    'plain_tasks_calculate_time_for_task', {
                        'started_matches': started_matches,
                        'toggle_matches': toggle_matches,
                        'now': now,
                        'eol': line.a + len(replacement) + len_dle}
                )
        self.view.sel().clear()
        for ind, pt in enumerate(original):
            ofs = ind * offset
            new_pt = sublime.Region(pt.a + ofs, pt.b + ofs)
            self.view.sel().add(new_pt)

        PlainTasksStatsStatus.set_stats(self.view)
        self.view.run_command('plain_tasks_toggle_highlight_past_due')


class PlainTasksInjectDueDateCommand(PlainTasksBase):
    def is_visible(self):
        return self.view.score_selector(0, "text.todo") > 0

    def runCommand(self, edit):
        due_re = r'^\s*[^\b]*?\s*@due\([\d\w,\.:\-\/ @]*\).*$'
        regions = itertools.chain(*(reversed(self.view.lines(region)) for region in reversed(list(self.view.sel()))))
        point = -1
        for line in regions:
            line_contents = self.view.substr(line)
            current_scope = self.view.scope_name(line.begin())
            due_matches = re.match(due_re, line_contents, re.U)
            if ('item' in current_scope or 'header' in current_scope) and not due_matches:
                self.view.insert(edit, line.end(), ' @due()')
                point = line.end() + 6
        if point != -1:
            self.view.sel().clear()
            self.view.sel().add(point)


class PlainTasksSortByDueDateAndPriorityCommand(PlainTasksBase):
    class Task:
        pass

    def is_visible(self):
        return self.view.score_selector(0, "text.todo") > 0

    def runCommand(self, edit, descending=False):
        due_re = r'^\s*[^\b]*?\s*@due\(([\d\w,\.:\-\/ @]*)\).*$'
        critical_re = r'^\s*[^\b]*?\s*@critical\b.*$'
        high_re = r'^\s*[^\b]*?\s*high\b.*$'
        low_re = r'^\s*[^\b]*?\s*@low\b.*$'
        regions = itertools.chain(*(reversed(self.view.lines(region)) for region in reversed(list(self.view.sel()))))

        for project in regions:
            project_scope = self.view.scope_name(project.begin())
            if 'header' not in project_scope:
                continue
            project_block = self.view.indented_region(project.end() + 1)
            if project_block.empty():
                continue
            tasks = []
            task = None
            pos = project_block.begin()
            first_task_pos = None
            first_task_indentation = None

            while pos < project_block.end():
                line = self.view.line(pos)
                line_contents = self.view.substr(line)
                scope = self.view.scope_name(pos)
                indentation = self.view.indentation_level(pos)

                if scope == 'text.todo notes.todo ' and task is None:
                    pass
                elif ('item' in scope or 'header' in scope) and (task is None or first_task_indentation == indentation):
                    task = self.Task()
                    task.region = line
                    if first_task_pos is None:
                        first_task_pos = pos
                        first_task_indentation = indentation

                    task.due = '99-01-01 00:00'
                    due_match = re.match(due_re, line_contents, re.U)
                    if due_match is not None:
                        task.due = due_match.groups()[0]

                    task.priority = '3normal'
                    critical_match = re.match(critical_re, line_contents, re.U)
                    high_match = re.match(high_re, line_contents, re.U)
                    low_match = re.match(low_re, line_contents, re.U)
                    if critical_match is not None:
                        task.priority = '1critical'
                    elif high_match is not None:
                        task.priority = '2high'
                    elif low_match is not None:
                        task.priority = '4low'

                    tasks.append(task)
                elif task is not None:
                    task.region = sublime.Region(task.region.begin(), line.end())
                pos = line.end() + 1
            tasks.sort(key=lambda t: (t.due, t.priority), reverse=descending)
            project_block = sublime.Region(first_task_pos, project_block.end())
            new_content = '\n'.join([self.view.substr(t.region).rstrip() for t in tasks]) + '\n'
            self.view.replace(edit, project_block, new_content)

        PlainTasksStatsStatus.set_stats(self.view)
        self.view.run_command('plain_tasks_toggle_highlight_past_due')


class PlainTasksCancelCommand(PlainTasksBase):
    def runCommand(self, edit):
        original = [r for r in self.view.sel()]
        canc_line_end, now = self.format_line_end(self.canc_tag, tznow())
        offset = len(canc_line_end)
        rom = r'^(\s*)(\[\s\]|.)(\s*.*)$'
        # rdm = r'^(\s*)(\[x\]|.)(\s*[^\b]*?(?:[^\@]|(?<!\s)\@|\@(?=\s))*?\s*)(?=((?:\s@done|@project|@[wl]asted|$).*)|(?:[ \t](\([^()]*\))\s*([^@]*|(?:@project|@[wl]asted).*))?$)'
        rcm = r'^(\s*)(\[\-\]|.)(\s*[^\b]*?(?:[^\@]|(?<!\s)\@|\@(?=\s))*?\s*)(?=((?:\s@cancelled|@project|@[wl]asted|$).*)|(?:[ \t](\([^()]*\))\s*([^@]*|(?:@project|@[wl]asted).*))?$)'
        started = r'^\s*[^\b]*?\s*@started(\([\d\w,\.:\-\/ @]*\)).*$'
        toggle = r'@toggle(\([\d\w,\.:\-\/ @]*\))'
        regions = itertools.chain(*(reversed(self.view.lines(region)) for region in reversed(list(self.view.sel()))))
        for line in regions:
            line_contents = self.view.substr(line)
            open_matches = re.match(rom, line_contents, re.U)
            # done_matches = re.match(rdm, line_contents, re.U)
            canc_matches = re.match(rcm, line_contents, re.U)
            started_matches = re.findall(started, line_contents, re.U)
            toggle_matches = re.findall(toggle, line_contents, re.U)

            canc_line_end = canc_line_end.rstrip()
            if line_contents.endswith('  '):
                canc_line_end += '  '  # keep double whitespace at eol
                dblspc = '  '
            else:
                dblspc = ''

            current_scope = self.view.scope_name(line.a)
            if 'pending' in current_scope:
                grps = open_matches.groups()
                len_cle = self.view.insert(edit, line.end(), canc_line_end)
                replacement = f'{grps[0]}{self.canc_tasks_bullet}{grps[2].rstrip()}'
                self.view.replace(edit, line, replacement)
                self.view.run_command(
                    'plain_tasks_calculate_time_for_task', {
                        'started_matches': started_matches,
                        'toggle_matches': toggle_matches,
                        'now': now,
                        'eol': line.a + len(replacement) + len_cle,
                        'tag': 'wasted'}
                )
            elif 'header' in current_scope:
                eol = self.view.insert(edit, line.end(), canc_line_end)
                self.view.run_command(
                    'plain_tasks_calculate_time_for_task', {
                        'started_matches': started_matches,
                        'toggle_matches': toggle_matches,
                        'now': now,
                        'eol': line.end() + eol,
                        'tag': 'wasted'}
                )
                indent = re.match('^(\s*)\S', line_contents, re.U)
                self.view.insert(edit, line.begin() + len(indent.group(1)), f'{self.canc_tasks_bullet} ')
                self.view.run_command('plain_tasks_calculate_total_time_for_project', {'start': line.a})
            elif 'completed' in current_scope:
                sublime.status_message('You cannot cancel what have been done, can you?')
                # replacement = f'{grps[0]}{self.canc_tasks_bullet}{grps[2]}{parentheses}'
                # self.view.replace(edit, line, replacement.rstrip())
                # offset = -offset
            elif 'cancelled' in current_scope:
                grps = canc_matches.groups()
                parentheses = check_parentheses(self.date_format, grps[4] or '')
                replacement = f'{grps[0]}{self.open_tasks_bullet}{grps[2]}{parentheses}'
                self.view.replace(edit, line, replacement.rstrip() + dblspc)
                offset = -offset
        self.view.sel().clear()
        for ind, pt in enumerate(original):
            ofs = ind * offset
            new_pt = sublime.Region(pt.a + ofs, pt.b + ofs)
            self.view.sel().add(new_pt)

        PlainTasksStatsStatus.set_stats(self.view)
        self.view.run_command('plain_tasks_toggle_highlight_past_due')


class PlainTasksArchiveCommand(PlainTasksBase):
    def runCommand(self, edit, partial=False):
        rds = 'meta.item.todo.completed'
        rcs = 'meta.item.todo.cancelled'

        # finding archive section
        archive_pos = self.view.find(self.archive_name, 0, sublime.LITERAL)

        if partial:
            all_tasks = self.get_archivable_tasks_within_selections()
        else:
            all_tasks = self.get_all_archivable_tasks(archive_pos, rds, rcs)

        if not all_tasks:
            sublime.status_message('Nothing to archive')
        else:
            if archive_pos and archive_pos.a > 0:
                line = self.view.full_line(archive_pos).end()
            else:
                create_archive = f'\n\n---- ✄ -----------------------\n\n\n## Archive (linear)\n\n{self.archive_name}\n'
                self.view.insert(edit, self.view.size(), create_archive)
                line = self.view.size()

            projects = get_all_projects_and_separators(self.view)

            # adding tasks to archive section
            for task in all_tasks:
                line_content = self.view.substr(task)
                match_task = re.match(r'^\s*(\[[x-]\]|.)(\s+.*$)', line_content, re.U)
                current_scope = self.view.scope_name(task.a)
                if rds in current_scope or rcs in current_scope:
                    pr = self.get_task_project(task, projects)
                    if self.project_postfix:
                        eol = f'{self.before_tasks_bullet_spaces}{line_content.strip()}{f" @project({pr}" if pr else ""}{")  " if line_content.endswith("  ") else ")"}\n'
                    else:
                        eol = f'{self.before_tasks_bullet_spaces}{match_task.group(1)}{f"{self.tasks_bullet_space}{pr}:" if pr else ""}{match_task.group(2)}\n'
                else:
                    eol = f'{self.before_tasks_bullet_spaces * 2}{line_content.lstrip()}\n'
                line += self.view.insert(edit, line, eol)

            # remove moved tasks (starting from the last one otherwise it screw up regions after the first delete)
            for task in reversed(all_tasks):
                self.view.erase(edit, self.view.full_line(task))
            self.view.run_command('plain_tasks_sort_by_date')

    def get_task_project(self, task, projects):
        index = -1
        for ind, pr in enumerate(projects):
            if task < pr:
                if ind > 0:
                    index = ind-1
                break
        #if there is no projects for task - return empty string
        if index == -1:
            return ''

        prog = re.compile(r'^\n*(\s*)(.+):(?=\s|$)\s*(\@[^\s]+(\(.*?\))?\s*)*')
        hierarhProject = ''

        if index >= 0:
            depth = re.match(r"\s*", self.view.substr(self.view.line(task))).group()
            while index >= 0:
                strProject = self.view.substr(projects[index])
                if prog.match(strProject):
                    spaces = prog.match(strProject).group(1)
                    if len(spaces) < len(depth):
                        hierarhProject = prog.match(strProject).group(2) + ((" / " + hierarhProject) if hierarhProject else '')
                        depth = spaces
                        if len(depth) == 0:
                            break
                else:
                    sep = re.compile(r'(^\s*)---.{3,5}---+$')
                    spaces = sep.match(strProject).group(1)
                    if len(spaces) < len(depth):
                        depth = spaces
                        if len(depth) == 0:
                            break
                index -= 1
        if not hierarhProject:
            return ''
        else:
            return hierarhProject

    def get_task_note(self, task, tasks):
        note_line = task.end() + 1
        while self.view.scope_name(note_line) == 'text.todo notes.todo ':
            note = self.view.line(note_line)
            if note not in tasks:
                tasks.append(note)
            note_line = self.view.line(note_line).end() + 1

    def get_all_archivable_tasks(self, archive_pos, rds, rcs):
        done_tasks = [i for i in self.view.find_by_selector(rds) if i.a < (archive_pos.a if archive_pos and archive_pos.a > 0 else self.view.size())]
        for i in done_tasks:
            self.get_task_note(i, done_tasks)

        canc_tasks = [i for i in self.view.find_by_selector(rcs) if i.a < (archive_pos.a if archive_pos and archive_pos.a > 0 else self.view.size())]
        for i in canc_tasks:
            self.get_task_note(i, canc_tasks)

        all_tasks = done_tasks + canc_tasks
        all_tasks.sort()
        return all_tasks

    def get_archivable_tasks_within_selections(self):
        all_tasks = []
        for region in self.view.sel():
            for ln in self.view.lines(region):
                line = self.view.line(ln)
                if ('completed' in self.view.scope_name(line.a)) or ('cancelled' in self.view.scope_name(line.a)):
                    all_tasks.append(line)
                    self.get_task_note(line, all_tasks)
        return all_tasks


class PlainTasksNewTaskDocCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.new_file()
        view.settings().add_on_change('color_scheme', lambda: self.set_proper_scheme(view))
        view.set_syntax_file('Packages/PlainTasks/PlainTasks.sublime-syntax')

    def set_proper_scheme(self, view):
        if view.id() != sublime.active_window().active_view().id():
            return
        pts = sublime.load_settings('PlainTasks.sublime-settings')
        if view.settings().get('color_scheme') == pts.get('color_scheme'):
            return
        # Since we cannot create file with syntax, there is moment when view has no settings,
        # but it is activated, so some plugins (e.g. Color Highlighter) set wrong color scheme
        view.settings().set('color_scheme', pts.get('color_scheme'))


class PlainTasksSortByDate(PlainTasksBase):
    def runCommand(self, edit):
        if not re.search(r'(?su)%[Yy][-./ ]*%m[-./ ]*%d\s*%H.*%M', self.date_format):
            # TODO: sort with dateutil so we wont depend on specific date_format
            return
        archive_pos = self.view.find(self.archive_name, 0, sublime.LITERAL)
        if archive_pos:
            have_date = r'(^\s*[^\n]*?\s\@(?:done|cancelled)\s*(\([\d\w,\.:\-\/ ]*\))[^\n]*$)'
            all_tasks_prefixed_date = []
            all_tasks = self.view.find_all(have_date, 0, "\\2\\1", all_tasks_prefixed_date)

            tasks_prefixed_date = []
            tasks = []
            for ind, task in enumerate(all_tasks):
                if task.a > archive_pos.b:
                    tasks.append(task)
                    tasks_prefixed_date.append(all_tasks_prefixed_date[ind])

            notes = []
            for ind, task in enumerate(tasks):
                note_line = task.end() + 1
                while self.view.scope_name(note_line) == 'text.todo notes.todo ':
                    note = self.view.line(note_line)
                    notes.append(note)
                    tasks_prefixed_date[ind] += f'\n{self.view.substr(note)}'
                    note_line = note.end() + 1

            to_remove = tasks+notes
            to_remove.sort()
            for i in reversed(to_remove):
                self.view.erase(edit, self.view.full_line(i))

            tasks_prefixed_date.sort(reverse=self.view.settings().get('new_on_top', True))
            eol = archive_pos.end()
            # Create a raw string for the regex pattern
            pattern = r"^\([\d\w,\.:\-\/ ]*\)([^\b]*$)"
            for a in tasks_prefixed_date:
                # Use the pattern in re.sub() outside the f-string
                replaced = re.sub(pattern, "\\1", a)
                eol += self.view.insert(edit, eol, f'\n{replaced}')
        else:
            sublime.status_message("Nothing to sort")


class PlainTasksRemoveBold(sublime_plugin.TextCommand):
    def run(self, edit):
        for s in reversed(list(self.view.sel())):
            a, b = s.begin(), s.end()
            for r in sublime.Region(b + 2, b), sublime.Region(a - 2, a):
                self.view.erase(edit, r)


class PlainTasksStatsStatus(sublime_plugin.EventListener):
    def on_activated(self, view):
        if not view.score_selector(0, "text.todo") > 0:
            return
        self.set_stats(view)

    def on_post_save(self, view):
        self.on_activated(view)

    @staticmethod
    def set_stats(view):
        view.set_status('PlainTasks', PlainTasksStatsStatus.get_stats(view))

    @staticmethod
    def get_stats(view):
        msgf = view.settings().get('stats_format', '$n/$a done ($percent%) $progress Last task @done $last')

        special_interest = re.findall(r'{{.*?}}', msgf)
        for i in special_interest:
            matches = view.find_all(i.strip('{}'))
            pend, done, canc = [], [], []
            for t in matches:
                # one task may contain same tag/word several times—we count amount of tasks, not tags
                t = view.line(t).a
                scope = view.scope_name(t)
                if 'pending' in scope and t not in pend:
                    pend.append(t)
                elif 'completed' in scope and t not in done:
                    done.append(t)
                elif 'cancelled' in scope and t not in canc:
                    canc.append(t)
            msgf = msgf.replace(i, '%d/%d/%d'%(len(pend), len(done), len(canc)))

        ignore_archive = view.settings().get('stats_ignore_archive', False)
        if ignore_archive:
            archive_pos = view.find(view.settings().get('archive_name', 'Archive:'), 0, sublime.LITERAL)
            pend = len([i for i in view.find_by_selector('meta.item.todo.pending') if i.a < (archive_pos.a if archive_pos and archive_pos.a > 0 else view.size())])
            done = len([i for i in view.find_by_selector('meta.item.todo.completed') if i.a < (archive_pos.a if archive_pos and archive_pos.a > 0 else view.size())])
            canc = len([i for i in view.find_by_selector('meta.item.todo.cancelled') if i.a < (archive_pos.a if archive_pos and archive_pos.a > 0 else view.size())])
        else:
            pend = len(view.find_by_selector('meta.item.todo.pending'))
            done = len(view.find_by_selector('meta.item.todo.completed'))
            canc = len(view.find_by_selector('meta.item.todo.cancelled'))
        allt = pend + done + canc
        percent  = ((done+canc)/float(allt))*100 if allt else 0
        factor   = int(round(percent/10)) if percent<90 else int(percent/10)

        barfull  = view.settings().get('bar_full', '■')
        barempty = view.settings().get('bar_empty', '□')
        progress = '%s%s' % (barfull*factor, barempty*(10-factor)) if factor else ''

        tasks_dates = []
        view.find_all('(^\s*[^\n]*?\s\@(?:done)\s*(\([\d\w,\.:\-\/ ]*\))[^\n]*$)', 0, "\\2", tasks_dates)
        date_format = view.settings().get('date_format', '(%y-%m-%d %H:%M)')
        tasks_dates = [check_parentheses(date_format, t, is_date=True) for t in tasks_dates]
        tasks_dates.sort(reverse=True)
        last = tasks_dates[0] if tasks_dates else '(UNKNOWN)'

        msg = (msgf.replace('$o', str(pend))
                   .replace('$d', str(done))
                   .replace('$c', str(canc))
                   .replace('$n', str(done+canc))
                   .replace('$a', str(allt))
                   .replace('$percent', str(int(percent)))
                   .replace('$progress', progress)
                   .replace('$last', last)
                )
        return msg


class PlainTasksCopyStats(sublime_plugin.TextCommand):
    def is_enabled(self):
        return self.view.score_selector(0, "text.todo") > 0

    def run(self, edit):
        msg = self.view.get_status('PlainTasks')
        replacements = self.view.settings().get('replace_stats_chars', [])
        if replacements:
            for o, r in replacements:
                msg = msg.replace(o, r)

        sublime.set_clipboard(msg)


class PlainTasksArchiveOrgCommand(PlainTasksBase):
    def runCommand(self, edit):
        # Archive the curent subtree to our archive file, not just completed tasks.
        # For now, it's mapped to ctrl-shift-o or super-shift-o

        # TODO: Mark any tasks found as complete, or maybe warn.

        # Get our archive filename
        archive_filename = self.__createArchiveFilename()

        # Figure out our subtree
        region = self.__findCurrentSubtree()
        if region.empty():
            # How can we get here?
            sublime.error_message("Error:\n\nCould not find a tree to archive.")
            return

        # Write our region or our archive file
        success = self.__writeArchive(archive_filename, region)

        # only erase our region if the write was successful
        if success:
            self.view.erase(edit,region)

        return

    def __writeArchive(self, filename, region):
        sublime.status_message(f'Archiving tree to {filename}')
        try:
            # Have to use io.open because windows doesn't like writing
            # utf8 to regular filehandles
            with io.open(filename, 'a', encoding='utf8') as fh:
                data = self.view.substr(region)
                # Is there a way to read this in?
                fh.write("--- ✄ -----------------------\n")
                fh.write(f"Archived {tznow().strftime(self.date_format)}:\n")
                # And, finally, write our data
                fh.write(f"{data}\n")
            return True

        except Exception as e:
            sublime.error_message(f"Error:\n\nUnable to append to {filename}\n{str(e)}")
            return False

    def __createArchiveFilename(self):
        # Create our archive filename, from the mask in our settings.

        # Split filename int dir, base, and extension, then apply our mask
        path_base, extension = os.path.splitext(self.view.file_name())
        dir  = os.path.dirname(path_base)
        base = os.path.basename(path_base)
        sep  = os.sep

        # Now build our new filename
        try:
            # This could fail, if someone messed up the mask in the
            # settings.  So, if it did fail, use our default.
            archive_filename = self.archive_org_filemask.format(
                dir=dir, base=base, ext=extension, sep=sep)
        except:
            # Use our default mask
            archive_filename = self.archive_org_default_filemask.format(
                    dir=dir, base=base, ext=extension, sep=sep)

            # Display error, letting the user know
            sublime.error_message(f"Error:\n\nInvalid filemask:{self.archive_org_filemask}\nUsing default: {self.archive_org_default_filemask}")

        return archive_filename

    def __findCurrentSubtree(self):
        # Return the region that starts at the cursor, or starts at
        # the beginning of the selection

        line = self.view.line(self.view.sel()[0].begin())
        # Start finding the region at the beginning of the next line
        region = self.view.indented_region(line.b + 2)

        if region.contains(line.b):
            # there is no subtree
            return sublime.Region(-1, -1)

        if not region.empty():
            region = sublime.Region(line.a, region.b)

        return region


class PlainTasksFoldToTags(PlainTasksFold):
    TAG = r'(?u)@\w+'

    def run(self, edit):
        tag_sels = [s for s in list(self.view.sel()) if 'tag.todo' in self.view.scope_name(s.a)]
        if not tag_sels:
            sublime.status_message('Cursor(s) must be placed on tag(s)')
            return

        tags = self.extract_tags(tag_sels)
        tasks = [self.view.line(f) for f in self.view.find_all(r'[ \t](%s)' % '|'.join(tags)) if 'pending' in self.view.scope_name(f.a)]
        if not tasks:
            sublime.status_message('Pending tasks with given tags are not found')
            print(tags, tag_sels)
            return
        self.exec_folding(self.add_projects_and_notes(tasks))

    def extract_tags(self, tag_sels):
        tags = []
        for s in tag_sels:
            start = end = s.a
            limit = self.view.size()
            while all(self.view.substr(start) != c for c in '@ \n'):
                start -= 1
                if start == 0:
                    break
            while all(self.view.substr(end) != c for c in '( @\n'):
                end += 1
                if end == limit:
                    break
            match = re.match(self.TAG, self.view.substr(sublime.Region(start, end)))
            tag =  match.group(0) if match else False
            if tag and tag not in tags:
                tags.append(tag)
        return tags


class PlainTasksAddGutterIconsForTags(sublime_plugin.EventListener):
    def on_activated(self, view):
        if not view.score_selector(0, "text.todo") > 0:
            return
        view.erase_regions('critical')
        view.erase_regions('high')
        view.erase_regions('low')
        view.erase_regions('today')
        icon_critical = view.settings().get('icon_critical', '')
        icon_high = view.settings().get('icon_high', '')
        icon_low = view.settings().get('icon_low', '')
        icon_today = view.settings().get('icon_today', '')
        if not any((icon_critical, icon_high, icon_low, icon_today)):
            return

        critical = 'string.other.tag.todo.critical'
        high = 'string.other.tag.todo.high'
        low = 'string.other.tag.todo.low'
        today = 'string.other.tag.todo.today'
        r_critical = view.find_by_selector(critical)
        r_high = view.find_by_selector(high)
        r_low = view.find_by_selector(low)
        r_today = view.find_by_selector(today)

        if not any((r_critical, r_high, r_low, r_today)):
            return
        view.add_regions('critical', r_critical, critical, icon_critical, sublime.HIDDEN)
        view.add_regions('high', r_high, high, icon_high, sublime.HIDDEN)
        view.add_regions('low', r_low, low, icon_low, sublime.HIDDEN)
        view.add_regions('today', r_today, today, icon_today, sublime.HIDDEN)

    def on_post_save(self, view):
        self.on_activated(view)

    def on_load(self, view):
        self.on_activated(view)


class PlainTasksHover(sublime_plugin.ViewEventListener):
    '''Show popup with actions when hover over bullet'''

    msg = ('<style>'  # four curly braces because it will be modified with format method twice
            'html {{{{background-color: color(var(--background) blenda(white 75%))}}}}'
            'body {{{{margin: .1em .3em}}}}'
            'p {{{{margin: .5em 0}}}}'
            'a {{{{text-decoration: none}}}}'
            'span.icon {{{{font-weight: bold; font-size: 1.3em}}}}'
            '#icon-done {{{{color: var(--greenish)}}}}'
            '#icon-cancel {{{{color: var(--redish)}}}}'
            '#icon-archive {{{{color: var(--bluish)}}}}'
            '#icon-outside {{{{color: var(--purplish)}}}}'
            '#done {{{{color: var(--greenish)}}}}'
            '#cancel {{{{color: var(--redish)}}}}'
            '#archive {{{{color: var(--bluish)}}}}'
            '#outside {{{{color: var(--purplish)}}}}'
           '</style><body>'
           '{actions}'
           )

    complete = '<a href="complete\v{point}"><span class="icon" id="icon-done">✔</span> <span id="done">Toggle complete</span></a>'
    cancel = '<a href="cancel\v{point}"><span class="icon" id="icon-cancel">✘</span> <span id="cancel">Toggle cancel</span></a>'
    archive = '<a href="archive\v{point}"><span class="icon" id="icon-archive">📚</span> <span id="archive">Archive</span></a>'
    archivetofile = '<a href="tofile\v{point}"><span class="icon" id="icon-outside">📤</span> <span id="outside">Archive to file</span></a>'

    actions = {
        'text.todo meta.item.todo.pending': '<p>{complete}</p><p>{cancel}</p>'.format(complete=complete, cancel=cancel),
        'text.todo meta.item.todo.completed': '<p>{archive}</p><p>{archivetofile}</p><p>{complete}</p>'.format(archive=archive, archivetofile=archivetofile, complete=complete),
        'text.todo meta.item.todo.cancelled': '<p>{archive}</p><p>{archivetofile}</p><p>{complete}</p><p>{cancel}</p>'.format(archive=archive, archivetofile=archivetofile, complete=complete, cancel=cancel)
    }

    @classmethod
    def is_applicable(cls, settings):
        return settings.get('syntax') == 'Packages/PlainTasks/PlainTasks.sublime-syntax'

    def on_hover(self, point, hover_zone):
        self.view.hide_popup()
        if hover_zone != sublime.HOVER_TEXT:
            return

        line = self.view.line(point)
        line_scope_name = self.view.scope_name(line.a).strip()
        if 'meta.item.todo' not in line_scope_name:
            return

        bullet = any(('bullet' in self.view.scope_name(p) for p in (point, point - 1)))
        if not bullet:
            return

        width, height = self.view.viewport_extent()
        self.view.show_popup(self.msg.format(actions=self.actions.get(line_scope_name)).format(point=point), 0, point or self.view.sel()[0].begin() or 1, width, height / 2, self.exec_action)

    def exec_action(self, msg):
        action, at = msg.split('\v')

        case = {
            'complete': lambda: self.view.run_command('plain_tasks_complete'),
            'cancel': lambda: self.view.run_command('plain_tasks_cancel'),
            'archive': lambda: self.view.run_command("plain_tasks_archive", {"partial": True}),
            'tofile': lambda: self.view.run_command('plain_tasks_org_archive'),
        }
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(int(at)))
        case[action]()
        self.view.hide_popup()


class PlainTasksGotoTag(sublime_plugin.TextCommand):
    def run(self, edit):
        self.initial_viewport = self.view.viewport_position()
        self.initial_sels = list(self.view.sel())

        self.tags = sorted(
            [r for r in self.view.find_by_selector('meta.tag.todo')
             if not any(s in self.view.scope_name(r.a) for s in ('completed', 'cancelled'))] +
            self.view.find_by_selector('string.other.tag.todo.critical') +
            self.view.find_by_selector('string.other.tag.todo.high') +
            self.view.find_by_selector('string.other.tag.todo.low') +
            self.view.find_by_selector('string.other.tag.todo.today')
        )

        window = self.view.window() or sublime.active_window()
        items = [[self.view.substr(t), f'{self.view.rowcol(t.a)[0]}: {self.view.substr(self.view.line(t)).strip()}']
                for t in self.tags]

        from bisect import bisect_left
        # find the closest tag after current viewport position to avoid scrolling
        closest_index = bisect_left([r.a for r in self.tags], self.view.layout_to_text(self.initial_viewport))
        llen = len(self.tags)
        selected_index = closest_index if closest_index < llen else llen - 1
        window.show_quick_panel(items, self.on_done, 0, selected_index, self.on_highlighted)

    def on_done(self, index):
        if index < 0:
            self.view.sel().clear()
            self.view.sel().add_all(self.initial_sels)
            self.view.set_viewport_position(self.initial_viewport)
            return

        self.view.sel().clear()
        self.view.sel().add(sublime.Region(self.tags[index].a))
        self.view.show_at_center(self.tags[index])

    def on_highlighted(self, index):
        self.view.sel().clear()
        self.view.sel().add(self.tags[index])
        self.view.show(self.tags[index], True)
