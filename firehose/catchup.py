import tyro
import requests
from bs4 import BeautifulSoup


def main(
    dates: list[str],
    /
):
    """
    Input: a list of string such as
    
        ["2025-04-23", "2025-04-24", "2025-04-25"]

    Output: a list of paper IDs from those dates.
    """
    for date in dates:
        url = "https://arxiv.org/catchup/cs/" + date
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')

        dl = soup.find('dl', id='articles')

        for i, dt in enumerate(dl.find_all('dt'), 1):
            a_tags = dt.find_all('a')
            paper_id = a_tags[1].text.strip()
            print(paper_id, date)


def cli():
    tyro.cli(main)
