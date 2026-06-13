# SSUET Survey Form Automation
Automates university course evaluation surveys using Python (Flask) backend and HTML frontend.

## Features
- Login with University Registration ID & Password
- Dashboard shows all courses pending survey
- Select lecturer per course
- Submit with **Best**, **Average**, or **Worst** ratings in one click

## Tech Stack
- **Backend:** Python (Flask, Requests/Selenium)
- **Frontend:** HTML, CSS, JavaScript


## Setup
```bash
git clone https://github.com/yourusername/ssuet-survey-automation.git
cd ssuet-survey-automation
pip install -r requirements.txt
python /app.py
```
Open `http://localhost:5000`

## Usage
1. Login with your reg ID and password
2. View your courses on the dashboard
3. Select a lecturer for each course
4. Choose **Best**, **Average**, or **Worst** and click **Submit All**


## Rating Modes
| Mode    | Rating Submitted     |
|---------|----------------------|
| Best    | Highest (e.g. 5/5)  |
| Average | Middle (e.g. 3/5)   |
| Worst   | Lowest (e.g. 1/5)   |
