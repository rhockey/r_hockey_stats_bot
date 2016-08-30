import asyncio
import configparser
import logging
import praw
import re
import redis
import requests
import sys
import time

from logging.handlers import RotatingFileHandler
from asyncio import Queue

# Constants
UPDATE_INTERVAL = 5 # reddit rate limits to 1 request ever 2 seconds
SUGGEST_API_PATH = "https://suggest.svc.nhl.com/svc/suggest/v1/minplayers/{}/300"
STATS_API_PATH = "https://statsapi.web.nhl.com/api/v1/people/{}?expand=person.stats&stats=yearByYear,careerRegularSeason&expand=stats.team&site=en_nhl" 
PER_THREAD_USER = 5
PER_THREAD = 25
NHL_LEAGUE_ID = 133


class HockeyStatsBot(object):

    reply_person = "{fullName} - {primaryPosition} - {primaryNumber} - {currentTeam}  \n"
    reply_skater_header = "Team|Season|Goals|Assists|Points|PIM|FO%|Games Played    \n--:|--:|--:|--:|--:|--:|--:|--:  \n"
    reply_skater_record = "[{abbreviation}]({officialSiteUrl})|{season}|{goals}|{assists}|{points}|{pim}|{faceOffPct}%|{games}    \n"
    reply_goalie_header = "Team|Season|Save %|GAA|Shutouts|Wins|Games Played    \n--:|--:|--:|--:|--:|--:|--:  \n"
    reply_goalie_record = "[{abbreviation}]({officialSiteUrl})|{season}|{savePercentage}|{goalAgainstAverage}|{shutouts}|{wins}|{games}    \n"

    def __init__(self, config_location, subreddit=None):
        """ Create the bot, parse the praw.ini, log into reddit,
        set up logger, set up redis connection

        Args:
            config_location (str): location of praw.ini on fs
            subreddit to use
        """

        config = configparser.ConfigParser()
        config.read(config_location)

        self.r = praw.Reddit(
            user_agent="linux:com.pacefalm.stats_bot:1.0.4 (by /u/pacefalmd)",
            site_name="hockey_stats_bot")

        if subreddit is not None:
            self.subreddit = self.r.subreddit("pacefalmd")
        else:
            self.subreddit = self.r.subreddit(
                config["hockey_stats_misc"]["subreddit"])

        self.redis = redis.StrictRedis()
        self.comment_queue = Queue(maxsize=1024)
        log_format = '%(asctime)-15s: %(message)s'

        logging.basicConfig(format=log_format)
        self.logger = logging.getLogger()
        self.logger.setLevel(logging.DEBUG)
        print(config)
        self.logger.addHandler(
            RotatingFileHandler(
                config["hockey_stats_misc"]["logging_dir"]
            )
        )

    def get_comments(self):
        """ Syncronously get a chunk of comments. Since PRAW is built
        off of requests, we cannot make this async
        
        Returns:
            list: Praw Comment objects
        """
        return self.subreddit.comments()

    def _regex_summon(self, comment):
        """ Search an individual comment body for [[\w*]]. This
        indicates a stats request. i.e. [[Rod Brind'Amour]]

        Args:
            comment (praw Comment): Comment to regex

        Returns:
            bool: True if string matches
        """
        return re.search("\[\[([^\[\]]*)\]\]", comment.body)

    def filter_comments(self):
        """ Filter comments by regex and by comments we've already replied to.
        Then dump the results into our async queue

        TODO: Yield multiple players
        """

        comments = self.get_comments()
        self.logger.debug('Retreived comments')
        for comment in comments:
            if self._regex_summon(comment) and self.check_db(comment):
                self.logger.debug(
                    "Scheduling task:\n\t %s\n\t Author: %s" % (
                        comment.body, comment.author))
                self.comment_queue.put_nowait(comment)

    async def filter_player(self):
        """ Asyncronously yield comments from the queue.
        Create a dict to pass on with the player
        first and last name, and the comment object itself. Then
        schedule a new task to get the player id

        Yields:
            get_player_ids(dict())
        """

        while not self.comment_queue.empty(): 
            comment = await self.comment_queue.get()
            parsed_comment = self._regex_summon(comment)

            def parse_names(parsed_comment):
                full_name = parsed_comment.group(0).replace("[[", "").replace("]]","")
                first_name = full_name.split(' ')[0].lower()
                last_name = (("".join(full_name.split(' ')[1:]))
                        #TODO: Handle special characters better
                        .lower()
                        .replace(".", "")
                        .replace("-", "")
                    )
                return first_name, last_name

            first_name, last_name = parse_names(parsed_comment)
            
            asyncio.ensure_future(self.get_player_ids({
                "comment": comment,
                "first_name": first_name,
                "last_name": last_name,
                }))

    async def get_player_ids(self, comment):
        """ Asyncronously get the player's id from nhl.com. Then
        yield it and the comment object forward to the
        get_player_stats method

        Yields:
            get_player_stats({})
        """

        self.logger.debug("Grabbing id for %s-%s" % (
            comment["first_name"], comment["last_name"].replace("'", "")))
        path = SUGGEST_API_PATH.format(comment["last_name"])
        response = requests.get(path)

        # Only worry about one player for now
        for player in response.json()["suggestions"]:
            if "{}-{}".format(
                    comment["first_name"],
                    comment["last_name"].replace("'", "")) in player:
                return asyncio.ensure_future(
                    self.get_player_stats({
                    "comment": comment["comment"],
                    "player_id": player.split("|")[0]}))
                

    async def get_player_stats(self, data):
        """ Asyncronously grab the players stats from the nhl api.
        Then yield this data and the comment object forward

        Yields:
            reply({})

        TODO: Switch requests with requests_future
        """

        self.logger.debug("Getting player stats for %s" % data["player_id"])
        path = STATS_API_PATH.format(data["player_id"])
        response = requests.get(path)
        return asyncio.ensure_future(self.reply({
            "comment": data["comment"], 
            "player_data": response.json()}))

    async def reply(self, data):
        """ Format response, check db again for the comment id to
        ensure that we don't reply multiple times, and reply

        TODO: Massive code cleanup. Switch to different string
        formatting utility
        """
        reply_msg = ""
        person = data["player_data"]["people"][0]
        self.logger.debug("Generating reply for request: %s" % (person["fullName"]))

        def format_reply(season_stats, career_stats, header, reply_format):
            # Start with hover constant. This needs to match the hover
            # Constant in the subreddit stylesheet
            reply_content = "#####&#009;\n\n######&#009;\n\n####&#009;  \n"
            reply_content += header
            reply_list = []
            counter = 0

            for season in season_stats[::-1]:
                if counter == 5:
                    break
                if "id" not in season["league"] or season["league"]["id"] != NHL_LEAGUE_ID:
                    continue
                try:
                    sstats = season['stat']
                    reply_list.append(reply_format.format(
                        abbreviation=season['team'].get(
                            'abbreviation', 'NHL'),
                        officialSiteUrl=season['team'].get(
                            'officialSiteUrl', 'http://nhl.com'),
                        season=season.get(
                            'season', ' '),
                        goals=sstats.get(
                            'goals', ' '),
                        assists=sstats.get(
                            'assists', ' '),
                        points=sstats.get(
                            'points', ' '),
                        pim=sstats.get(
                            'pim', ' '),
                        faceOffPct=sstats.get(
                            'faceOffPct', ' '),
                        games=sstats.get(
                            'games', ' '),
                        savePercentage=sstats.get(
                            'savePercentage', ' '),
                        goalAgainstAverage=sstats.get(
                            'goalAgainstAverage', ' '),
                        shutouts=sstats.get(
                            'shutouts', ' '),
                        wins=sstats.get(
                            'wins', ' '),
                        ))
                except Exception:
                    self.logger.exception('Error parsing stats')
                counter += 1
            for reply_rec in reply_list[::-1]:
                reply_content += reply_rec
            try:
                career_stats_dict = career_stats["splits"][0]
                cstats = career_stats_dict['stat']
                reply_content += reply_format.format(
                        abbreviation="NHL",
                        officialSiteUrl="http://nhl.com",
                        season="Career",
                        goals=cstats.get(
                            'goals', ' '),
                        assists=cstats.get(
                            'assists', ' '),
                        points=cstats.get(
                            'points', ' '),
                        pim=cstats.get(
                            'pim', ' '),
                        faceOffPct=cstats.get(
                            'faceOffPct', ' '),
                        games=cstats.get(
                            'games', ' '),
                        savePercentage=cstats.get(
                            'savePercentage', ' '),
                        goalAgainstAverage=cstats.get(
                            'goalAgainstAverage', ' '),
                        shutouts=cstats.get(
                            'shutouts', ' '),
                        wins=cstats.get(
                            'wins', ' '),
                        )
            except Exception:
                self.logger.exception('Error Parsing career stats')
            return reply_content

        season_stats = [s["splits"] for s in person["stats"] if s["type"]["displayName"] == "yearByYear"][0]
        career_stats = [s for s in person["stats"] if s["type"]["displayName"] == "careerRegularSeason"][0]

        reply_msg += self.reply_person.format(
                fullName=person.get('fullName', ' '),
                primaryPosition=person.get('primaryPosition', {}).get('name', ' '),
                primaryNumber=person.get('primaryNumber', ' '),
                currentTeam=person.get('currentTeam', {}).get('name', 'N/A')
                )
        if person["primaryPosition"]["code"] == "G":
            reply_msg += format_reply(season_stats, career_stats, self.reply_goalie_header, self.reply_goalie_record)
        else:
            reply_msg += format_reply(season_stats, career_stats, self.reply_skater_header, self.reply_skater_record)
        reply_msg += "\n\n"
        reply_msg += "^^^issues? ^^^contact ^^^/u/pacefalmd"

        # Check again incase we've pulled two posts by the same user
        if self.check_db(data["comment"], update=True): 
            if reply_msg:
                data["comment"].reply(reply_msg)

    def check_db(self, comment, update=False):
        """ Check redis for the comment.id and the commentor.author.
        If we find the comment.id, return false.
        If we don't:
            If we find the comment.author for that and they have
            exceeded the max comments per thread, return False.

            If we find the comment.author for that and they have
            not exceeded the max comments per thread, increment that
            number and return true

            If we don't find the comment.author for that, set the 
            value to 1 and return True

        Stored value in form:
        {submission_id: {user_id:count}}

        Args:
            comment (Praw comment): comment to check for
            update (bool): optional kwarg to not update the db

        Returns:
            bool: Continue on with processing
        """

        submission = comment.submission.id
        user = comment.author.id
        id = comment.id 
        id_string = "id-{}".format(id)

        if self.redis.exists(id_string):
            self.logger.debug("Already responded")
            return False #Already responded

        if update:
            self.redis.set(id_string, 1)

        if user == "fhdgl":
            self.logger.debug("Responding to daddy")
            return True

        # No posts in thread, pass
        if not self.redis.hexists(submission, 'total'): 
            self.redis.hmset(submission, {user: 0, 'total':0})
            return True
        else:
            total = int(self.redis.hget(submission, 'total'))
            user_count = self.redis.hget(submission, user)
            if user_count == None:
                user_count = 0
            else:
                user_count = int(user_count)
            print(total, user_count)
            if total >= PER_THREAD: # Too many posts in one thread
                self.logger.debug("Thread %s is spammed" % submission)
                return False
            if user_count >= PER_THREAD_USER:
                self.logger.debug("User %s is spamming" % user)
                return False # User is spamming
            if update:
                self.logger.debug("Updating thread %s" % submission)
                self.redis.hincrby(submission, user)
                self.redis.hincrby(submission, total)
            return True # Pass

    def main(self):
        """ Main loop. Get comments, async the responses, sleep.
        """
        try:
            self.filter_comments()
            yield from asyncio.ensure_future(self.filter_player())
            yield from asyncio.sleep(10)
        except Exception as e:
            self.logger.exception("Exception")
            pass

#TODO: Better arg parsing
x = HockeyStatsBot(sys.argv[1], sys.argv[2])
now = time.time
sleep_until = UPDATE_INTERVAL + now()
loop = asyncio.get_event_loop()
while True:
    loop.run_until_complete(x.main())

