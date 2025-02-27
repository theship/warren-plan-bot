#!/usr/bin/env python3

import json
import os
import time

import click
import praw
import praw.models
from google.cloud import firestore

import pushshift
import reddit_util
from plan_bot import process_post

# JSON filename of policy plans
PLANS_FILE = "plans.json"
PLANS_CLUSTERS_FILE = "plan_clusters.json"

TIME_IN_LOOP = os.getenv(
    "TIME_IN_LOOP", 40
)  # seconds to spend in loop when calling from event handler. this should be less than the time between running iterations of the cloud function

click_kwargs = {"show_envvar": True, "show_default": True}


@click.command()
@click.option(
    "--send-replies/--skip-send",
    envvar="SEND_REPLIES",
    default=False,
    is_flag=True,
    help="whether to send replies",
    **click_kwargs,
)
@click.option(
    "--skip-tracking",
    envvar="SKIP_TRACKING",
    default=False,
    is_flag=True,
    help="whether to check whether replies have already been posted",
    **click_kwargs,
)
@click.option(
    "--simulate-replies",
    default=False,
    is_flag=True,
    help="pretend to make replies, including updating state",
    **click_kwargs,
)
@click.option(
    "--limit",
    envvar="LIMIT",
    type=int,
    default=10,
    help="number of posts to return",
    **click_kwargs,
)
@click.option(
    "--praw-site",
    envvar="PRAW_SITE",
    type=click.Choice(["dev", "prod"]),
    default="dev",
    help="section of praw file to use for reddit module configuration",
    **click_kwargs,
)
@click.option(
    "--project",
    envvar="GCP_PROJECT",
    default="wpb-dev",
    type=str,
    help="gcp project where firestore db lives",
    **click_kwargs,
)
def run_plan_bot(
    send_replies=False,
    skip_tracking=False,
    simulate_replies=False,
    limit=10,
    praw_site="dev",
    project="wpb-dev",
):
    """
    Run a single pass of Warren Plan Bot

    \b
    - Check list of posts replied to (If tracking is on)
    - Search for any new comments and submissions not on that list
    - Reply to any unreplied matching comments (If replies are on)
    - Update replied_to list (If replies and tracking is on)
    """
    print("Running a single pass of plan bot")
    pass_start_time = time.time()

    if simulate_replies and send_replies:
        raise ValueError(
            "--simulate-replies and --send-replies options are incompatible. at most one may be set"
        )

    # Change working directory so that praw.ini works, and so all files can be in this same folder. FIXME
    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    # change dev to prod to shift to production bot
    reddit = praw.Reddit(praw_site)

    # Ensure that we don't accidentally write to Reddit
    reddit.read_only = not send_replies

    with open(PLANS_FILE) as json_file:
        pure_plans = json.load(json_file)

    with open(PLANS_CLUSTERS_FILE) as json_file:
        plan_clusters = json.load(json_file)

    for plan in plan_clusters:
        plan["is_cluster"] = True
        plan["plans"] = [
            next(filter(lambda p: p["id"] == plan_id, pure_plans))
            for plan_id in plan["plan_ids"]
        ]

    plans = pure_plans + plan_clusters

    if skip_tracking:
        posts_db = None
        post_ids_processed = {}
    else:
        db = firestore.Client(project=project)

        posts_db = db.collection("posts")

        # Load the list of posts replied to or start with empty list if none
        posts_replied_to = posts_db.where("replied", "==", True).stream()
        # TODO migrate posts replied=True to have processed=True, and remove the query above (#84)
        posts_processed = posts_db.where("processed", "==", True).stream()

        # include processed posts in replied to
        post_ids_processed = {post.id for post in posts_replied_to}.union(
            {post.id for post in posts_processed}
        )

    subreddit_name = "ElizabethWarren" if praw_site == "prod" else "WPBSandbox"

    # Get the subreddit
    subreddit = reddit.subreddit(subreddit_name)

    # Get the number of new submissions up to the limit
    # Note: If this gets slow, we could switch this to pushshift
    for submission in subreddit.search(
        "warrenplanbot", sort="new", time_filter="all", limit=limit
    ):
        # turn this into our more standardized class
        submission = reddit_util.Submission(submission)
        process_post(
            submission,
            plans,
            posts_db,
            post_ids_processed,
            send=send_replies,
            simulate=simulate_replies,
            skip_tracking=skip_tracking,
        )

    for pushshift_comment in pushshift.search_comments(
        "warrenplanbot", subreddit_name, limit=limit
    ):

        comment = reddit_util.Comment(
            praw.models.Comment(reddit, _data=pushshift_comment)
        )

        process_post(
            comment,
            plans,
            posts_db,
            post_ids_processed,
            send=send_replies,
            simulate=simulate_replies,
            skip_tracking=skip_tracking,
        )

    print(f"Single pass of plan bot took: {round(time.time() - pass_start_time, 2)}s")


def run_plan_bot_event_handler(event, context):
    start_time = time.time()
    print("Starting plan bot loop")
    while time.time() - start_time < TIME_IN_LOOP:
        # Click exits with return code 0 when everything worked. Skip that behavior
        try:
            run_plan_bot(
                prog_name="run_that_plan_bot"
            )  # need to set prog_name to avoid weird click behavior in cloud fn
        except SystemExit as e:
            if e.code != 0:
                raise
        # add a sleep so things don't go crazy if we make things very fast at some point
        # for example, pushshift has a rate limit that we don't want to hit https://api.pushshift.io/meta
        time.sleep(1)


if __name__ == "__main__":
    run_plan_bot()
