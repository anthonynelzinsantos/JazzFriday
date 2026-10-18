# Jazz Friday

A weekly harvest of new jazz releases picked by Qobuz’s editors, with Apple Music links for auditioning.

Standard library only (Python 3.9+). Runs every Saturday morning from cron. It fetches the
Qobuz jazz shop page, records any album it has not seen before, and rewrites static pages grouped by release week. The front page lists the latest weeks, previous weeks go under `/archive`. Nothing is ever deleted from the state file. Covers are cached next to it, in `covers/`, as 300 and 600 pixel wide JPEGs and served from the site, so pages do not depend on Qobuz’s CDN. Use at your own risk.

A live demo is available at [`https://jazz.jonesa.fr`](https://jazz.jonesa.fr).

## Usage

	python3 jazz_friday.py --out-dir /var/www/jazz --state /var/lib/jazz-friday/state.json
	
## License

EUPL v1.2