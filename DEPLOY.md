# Deploying to Streamlit Cloud

This puts the dashboard on a URL your colleagues can open, and moves the daily
update off your laptop onto GitHub's servers.

Everything here has been built and tested locally. The steps below are the ones
only you can do, because they need your GitHub and Streamlit accounts.

## Before you start: two decisions

**1. Is external hosting acceptable?** The code and `rates.db` would live on
GitHub and Streamlit (Snowflake) infrastructure, outside the ib vogt tenant.
The rates themselves are public market data, published openly by PDEx, BNM and
the NY Fed, so the data is not sensitive. What the repository does reveal is
which markets ib vogt tracks. Confirm this against ib vogt's data governance
rules before pushing. If the answer is no, the SharePoint route we discussed
stays available and keeps everything inside the tenant.

**2. Who administers access?** Streamlit Community Cloud private apps invite
viewers by email address, managed by you rather than by IT. Colleagues sign in
with a Google or email account, not their ib vogt SSO. That is real
authentication, but it sits outside Entra ID, so IT will not see or control the
access list.

## Step 1: create a private GitHub repository

Make it **private**. Streamlit Community Cloud can deploy from private
repositories.

**The whole folder is safe to upload.** The PDEx API key is not stored in it -
it lives in the `PDEX_API_KEY` Windows environment variable. So whether you drag
every file into GitHub's web uploader or push from the command line, nothing
secret goes with it.

To push from the command line:

```bash
git remote add origin https://github.com/<your-account>/<repo-name>.git
```

```bash
git push -u origin main
```

To upload through the browser instead: **Add file > Upload files**, select
everything in `C:\Users\MJkang\rates-db`, drag it in, and commit.

Two things to check afterwards:

- The **`.github`** folder arrived, with `workflows/daily-update.yml` inside it.
  Some browsers drop folders whose name starts with a dot, and that file is what
  runs the daily update.
- The **`data`** folder arrived, with five CSV files in it. That is the actual
  rate data. Without it the app has nothing to show.

**Do not upload `rates.db`.** It is gitignored deliberately. It is a local build
artefact, and pushing a 9 MB SQLite binary through the web uploader corrupts it -
the app then fails with `sqlite3.DatabaseError` on its first query. If a
`rates.db` is already sitting in the repository from an earlier attempt, delete
it there.

## Step 2: add the PDEx key as a GitHub secret

In the repository: **Settings > Secrets and variables > Actions > New
repository secret**.

- Name: `PDEX_API_KEY`
- Value: your key. To read it back, run this in PowerShell:
  `[Environment]::GetEnvironmentVariable("PDEX_API_KEY","User")`

Without this the daily workflow will still run, but BVAL will report
"No PDEx API key configured" and only KLIBOR and SOFR will update.

## Step 3: check the workflow runs

Go to the **Actions** tab, pick "Daily rates update", and click **Run
workflow** to trigger it by hand rather than waiting for the cron.

It fetches the latest rates, runs the contract tests, folds the write-ahead log
into `rates.db`, and commits the file only if something changed. On a weekend
or a public holiday it will correctly report no new rates and commit nothing.

The schedule is 09:30 Manila and Kuala Lumpur time on weekdays, which is later
than your current 08:15 task, so BVAL and KLIBOR have published by then.

## Step 4: deploy on Streamlit Cloud

1. Go to <https://share.streamlit.io> and sign in with the same GitHub account.
2. **Create app**, pick the repository, branch `main`, main file
   `streamlit_app.py`.
3. Deploy. The first build installs `requirements.txt`, which takes a few
   minutes.

The app needs no secrets of its own. It only reads `rates.db` from the
repository and never fetches from a source, so the PDEx key stays in GitHub
Actions.

## Step 5: make the app private and invite colleagues

In the app's **Settings > Sharing**, set it to private and add your colleagues'
email addresses. Anyone not on the list gets a sign-in wall rather than the
dashboard.

## Step 6: turn off the Windows scheduled task

Once the Actions workflow has run successfully a few times, retire the local
one so the database is not being written from two places:

```bash
powershell -ExecutionPolicy Bypass -File install_task.ps1 -Remove
```

The local app keeps working. `python serve.py` still reads the same `rates.db`,
which now arrives via `git pull` instead of a local fetch.

## How the pieces fit together afterwards

```
GitHub Actions (weekdays 09:30 MYT/PHT)
    runs cli.py update  ->  fetches PDEx, BNM, NY Fed
    runs the contract tests
    commits rates.db back to the repository
                |
                v
Streamlit Cloud redeploys on each commit
    streamlit_app.py reads rates.db (read-only)
                |
                v
Colleagues open one URL and sign in
```

## Things to watch

- **Streamlit Community Cloud sleeps idle apps.** The first visit after a quiet
  period takes a few seconds to wake. Data is unaffected.
- **The repository grows over time.** `rates.db` is 7.1 MB and gains roughly
  40 rows a day, so growth is slow, but every daily commit stores a new copy of
  the file. If the repository becomes large after a year or two, squash the
  history or switch the workflow to commit CSVs instead of the database.
- **GitHub disables scheduled workflows in repositories with no activity for
  60 days.** The daily commits count as activity, so this only matters if every
  source stops publishing at once.
- **Two writers would conflict.** Do not leave the Windows task running once
  Actions is live, or you will get merge conflicts on `rates.db`.

## If you would rather not use external hosting

The SharePoint route keeps everything inside the ib vogt tenant: your machine
stays the single writer, publishes an Excel workbook and CSVs to
`04. Project Finance/0. General/00. Claude/Benchmark Rates`, and colleagues get
access through their normal ib vogt login. Say the word and I will build that
instead. The two are not mutually exclusive.
