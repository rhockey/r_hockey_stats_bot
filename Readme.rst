Hockey Stats bot.
-----------------

Requires python 3.5, praw4

Quickstart:
-----------

Create a virtualenv::

    mkvirtualenv my_env -p $(which python3.5)

Bootstrap it::

    . ./bootstrap

Run it::

    python statsbot.py $PRAW.INI_LOCATION $SUBREDDIT
    
Usage:

Post in /r/hockey (or your subreddit) with the following format [[Player Name]].
It will reply to you with a specific string used by the stylesheet to enable
hovering, along with the players stats.
