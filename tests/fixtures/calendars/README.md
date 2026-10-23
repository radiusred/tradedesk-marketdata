# Synthetic release-calendar fixtures

Every file here is **synthetic test data**. Each one has the shape of a
format that `tradedesk-md-calendars` reads (an iCalendar file and a curated
delimited file) or keeps (a source page), but every institution, title, date
and time is made up. They are not real publications.

- `synthetic_calendar.ics`: CRLF lines, a `VTIMEZONE` block and six events:
  2024-01-10 12:00 with a `TZID` (its title has an escaped comma and is
  folded over two lines); 2024-01-24 13:00 UTC; 2024-02-07 all day;
  2024-02-14 `STATUS:CANCELLED`; 2024-02-21 09:30 floating (no zone);
  2024-03-01 all day with a nested `VALARM`, whose title no type rule in the
  tests matches.
- `synthetic_calendar_later.ics`: a later download of the same calendar,
  2024-01-24 to 2024-03-20: the 2024-01-24 event moves to 14:00 UTC, the
  2024-02-07 event is gone, the 2024-02-21 event is unchanged, and a new
  event is on 2024-03-20.
- `synthetic_releases.csv`: a title line, then `;`-separated columns
  `Day;Time;Zone;Release;Kind` with `DD/MM/YYYY` dates: a row with a time
  and no zone, a row with a time and a zone (its title has a double space), a blank line, and a
  row with neither time nor zone.
- `synthetic_page.html`: a source page to keep raw beside the curated file.
