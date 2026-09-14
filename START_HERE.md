# Start here

This is the dashboard version. Ignore the earlier Windows/local scheduler instructions.

1. Create a GitHub repository and upload this folder's **contents**, including `.github` and `.streamlit`. `app.py` must be at the repository root.
2. Open **Actions > Refresh PoorStock decks** to check the first collection. The initial push starts it automatically. If no run starts, click **Run workflow** once.
3. In Streamlit Community Cloud, choose **Create app**, select that repository/default branch, set the main file to **`app.py`**, select Python **3.12**, and deploy.

Once deployed, GitHub collects links daily at **11 a.m. EDT (15:00 UTC)** and commits the saved data. Streamlit displays it. Your computer does not need to remain on. GitHub can start scheduled jobs late; the dashboard changes after collection finishes.

The initial data files are empty. The first successful collection fills the dashboard with available links associated with recent meetings. Subsequent runs also identify newly observed links. The visible window rolls forward automatically over 30 days.

This package has **82 passing offline tests**. Actual live collection, Streamlit rendering, and deployment still need the first GitHub runs to validate them. See README.md for details and troubleshooting.
