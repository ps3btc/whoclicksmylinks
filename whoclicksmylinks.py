#!/usr/bin/env python
#
# Copyright 2010 Hareesh Nagarajan.

__author__ = 'hareesh.nagarajan@gmail.com (Hareesh Nagarajan)'

import os
import sys
import urllib
import urllib2
import time
import logging
import calendar
import datetime
import wsgiref.handlers

import celebs

from django.utils import simplejson as json
from google.appengine.api import memcache
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template

DATETIME_STRING_FORMAT = '%a %b %d %H:%M:%S +0000 %Y'
BITLY_KEY = 'R_5e2a59607054db4cf2dc101cd84bf4fd'
BITLY_LOGIN = 'hnag'
ALL_USERS_LIST = '6376097130910121270'

class BitlyError(Exception):
  pass

class InvalidUserError(Exception):
  pass

class ProtectedUserError(Exception):
  pass

class TwitterError(Exception):
  pass

def format_text(text):
  tokens = text.split()
  formatted = []
  for token in tokens:
    if token.find('@') == 0:
      at = '@<a class="user" href="/u/%s">%s</a>' % (token[1:], token[1:])
      formatted.append(at)
    elif token.find('#') == 0:
      hashtag = '<a class="hashtag" href="http://search.twitter.com/search?q=%%23%s">%s</a>' % (token[1:], token)
      formatted.append('%s' % hashtag)
    elif token.find('http://') == 0:
      http_link = '<a class="httplink" href="%s">%s</a>' % (token, token)
      formatted.append(http_link)
    else:
      formatted.append(token)
  return ' '.join(formatted)

def get_time_ago(reference_epoch, created_at):
  tww=time.strptime(created_at, DATETIME_STRING_FORMAT)
  seconds_ago = int(reference_epoch - calendar.timegm(tww))
  ret = '%d secs ago' % seconds_ago
  if seconds_ago > 60 and seconds_ago <= 3600:
    ret = '%d mins ago' % (seconds_ago / 60)
  elif seconds_ago > 3600 and seconds_ago <= 86400:
    ret = '%d hrs ago' % (seconds_ago / 3600)
  elif seconds_ago > 86400:
    ret = '%d days ago' % (seconds_ago / 86400)
  return '%s' % ret

def extract_bitly_shortcut(text):
  for word in text.split():
    if word.find('http://bit.ly') != -1 or word.find('http://j.mp') != -1:
      return word.split('/')[-1:][0]
  return None

def get_clicks(shortcut):
  url = ('http://api.bit.ly/stats?version=2.0.1&hash=%s&login=%s&apiKey=%s'
         % (shortcut, BITLY_LOGIN, BITLY_KEY))
  try:
    handle = urllib2.urlopen(url)
  except:
    logging.error('Cannot fetch %s', url)
    raise BitlyError
  
  data = json.loads(handle.read())
  clicks = None
  try:
    clicks = int(data['results']['userClicks'])
  except:
    clicks = int(data['results']['clicks'])
  return clicks

class ReportEntry:
  def __init__(self, clicks, followers, time_ago_str, text):
    self.clicks = commaify(clicks)
    self.followers = commaify(followers)
    self.time_ago_str = time_ago_str
    self.text = format_text(text)
    self.clickthrough = '%.2f%%' % ((clicks / followers) * 100.0)

class Report(db.Model):
  username = db.StringProperty(indexed=True)
  last_updated = db.DateTimeProperty(auto_now_add=True, required=True)
  # This is very bad, but I'm very lazy. Let us just store the entire
  # HTML page here. How gross is that?
  page = db.BlobProperty()

def does_user_report_exist(user):
  result_list = db.GqlQuery(
      "SELECT * FROM Report WHERE username = :u LIMIT 1",
      u=user)

  for result in result_list:
    logging.info('Found user in db %s (%s %d)',
                 user,
                 result.last_updated,
                 len(result.page))
    return (result.last_updated, result.page)
  
  logging.info('Did not find user in db %s', user)
  return (None, None)

def add_user_report(user, page):
  # First delete all entries (there must only be 1) for the user.
  result_list = db.GqlQuery(
      "SELECT * FROM Report WHERE username = :u",
      u=user)
  db.delete(result_list)

  # Now, add the entry.
  obj = Report()
  obj.username = user
  obj.page = db.Blob(page)
  db.put(obj)
  logging.info('Wrote record for %s (page size %d %d)',
               user, len(page), len(obj.page))

def get_users_in_db():
  user_list = []
  result_list = db.GqlQuery("SELECT * FROM Report ORDER BY last_updated DESC LIMIT 100")
  for result in result_list:
    user_list.append(result.username)
  return user_list

class Summary:
  def __init__(self, user, total_links, total_clicks, followers):
    self.user = user
    self.total_links = commaify(total_links)
    self.total_clicks = commaify(total_clicks)
    self.followers = commaify(int(followers))

def commaify(value):
  try:
    int(value)
  except ValueError:
    return str(value)

  value = str(value)
  if value.find('.') != -1 or len(value) <= 3:
    return value
  return ''.join(commaify(value[:-3]) + ',' + value[-3:])

def get_bitly_tweets(user):
  url = 'http://twitter.com/statuses/user_timeline/%s.json?count=200' % user
  try:
    handle=urllib2.urlopen(url)
  except urllib2.HTTPError, err:
    if err.code == 404:
      logging.error('Failed to fetch, Invalid User: %s', url)
      raise InvalidUserError
    elif err.code == 401:
      logging.error('User has protected twitter feed %s', url)
      raise ProtectedUserError
    else:
      logging.error('Twitter error %s (%s)', url, err.code)
      raise TwitterError

  results = []
  reference_epoch = time.time()
  data = json.loads(handle.read())
  followers_count = float(data[0]['user']['followers_count'])
  total_clicks = 0
  total_links = 0
  for result in data:
    text = result['text']
    if (text.find('bit.ly') != -1 or text.find('j.mp') != -1) and text.find('RT') == -1:
      time_ago_str = get_time_ago(reference_epoch, result['created_at'])
      shortcut = extract_bitly_shortcut(text)
      if shortcut:
        clicks = get_clicks(shortcut)
        if clicks:
          total_clicks += clicks
          total_links += 1
          results.append(ReportEntry(clicks, followers_count, time_ago_str, text))

  summary = Summary(user, total_links, total_clicks, followers_count)
  return results, summary

def add_to_recent_users(user):
  all_users_list = memcache.get(ALL_USERS_LIST)

  if all_users_list is None:
    logging.info('memcache: Empty ALL_USERS_LIST adding first user %s', user)
    memcache.add(ALL_USERS_LIST, [ user ], 86400)
    return
    
  if user not in all_users_list:
    all_users_list.append(user)
    memcache.delete(ALL_USERS_LIST)
    memcache.add(ALL_USERS_LIST, all_users_list, 86400)
    logging.info('memcache: Adding user to ALL_USERS_LIST %s (%d)',
                 user, len(all_users_list))
  else:
    logging.info('memcache: User already in ALL_USERS_LIST %s', user)

def get_recent_users():
  # Screw memcache, just get the users in the DB.
  return get_users_in_db()
  
  recent_users = memcache.get(ALL_USERS_LIST)
  if recent_users:
    recent_users.reverse()
  return recent_users

class Home(webapp.RequestHandler):
  def get(self):
    stats = memcache.get_stats()
    username = self.request.get('username')
    if username and len(username) > 0:
      self.redirect('/u/%s' % username, permanent=False)
      return
    path = os.path.join(os.path.dirname(__file__), 'home.html')
    self.response.out.write(template.render(path, {
        'show_why': True,
        'recent_users': get_recent_users()[:10],
        'error_text' : None,
        }))

class About(webapp.RequestHandler):
  def get(self):
    path = os.path.join(os.path.dirname(__file__), 'about.html')
    self.response.out.write(template.render(path, {
        'show_why': False
        }))

class Celebs(webapp.RequestHandler):
  def get(self):
    path = os.path.join(os.path.dirname(__file__), 'celebrities.html')
    self.response.out.write(template.render(path, {
        'show_why': False,
        'celebrities' : celebs.CELEBS,
        }))    

class FlushMemcache(webapp.RequestHandler):
  def get(self):
    if memcache.flush_all():
      logging.info('memcache: flushed all')

class FlushDb(webapp.RequestHandler):
  def get(self):
    #
    # Dangerous! FOR TESTING ONLY!
    #
    result_list = db.GqlQuery("SELECT * FROM Report")
    db.delete(result_list)
    logging.info('deleted everything from DB!')

class User(webapp.RequestHandler):
  def show_home_error(self, error_text):
    path = os.path.join(os.path.dirname(__file__), 'home.html')
    self.response.out.write(template.render(path, {
        'show_why': True,
        'recent_users': get_recent_users(),
        'error_text' : error_text,
        }))
  
  def get(self, username):
    # 1) try to fetch the user if the user's page exists in memcache.
    username = unicode(urllib.unquote(username), 'utf-8')

    # Remove the '@' from the username if it exists
    if username[0] == '@':
      logging.info('Removing the leading @ from the username %s', username)
      username = username[1:]
      
    render_page = memcache.get(username)
    if render_page:
      self.response.out.write(render_page)
      logging.info('memcache: retrieve results for %s', username)
      return

    # 2) try and fetch the user's page from the datastore.
    (last_updated, render_page) = does_user_report_exist(username)
    if last_updated and render_page:
      self.response.out.write(render_page)
      return

    # 3) Make urlfetch requests to twitter and bit.ly.
    created_at = datetime.datetime.now()
    result_list = None
    summary = None
    try:
      result_list, summary = get_bitly_tweets(username)
    except TwitterError:
      return self.show_home_error(
          'oh noes! our connection to twitter broke. try again?')
    except ProtectedUserError:
      return self.show_home_error(
          'NO DONUT4U! %s has protected their twitter feed.' % username)
    except InvalidUserError:
      return self.show_home_error(
          'oh noes! %s does not exist in twitter! u know that :)' % username)
    except BitlyError:
      return self.show_home_error(
          'oh noes! our connection to bit.ly broke. try again?')
    add_to_recent_users(username)
    path = os.path.join(os.path.dirname(__file__), 'user.html')
    template_values = {
        'result_list' : result_list,
        'username' : username,
        'summary' : summary,
        'created_at' : created_at,
        'show_why': False,
    }

    # Prepare the page to render
    render_page = template.render(path, template_values)

    # Add the user -> page to memcache.
    memcache.add(username, render_page, 86400)

    # Add the user -> page to the datastore.
    add_user_report(username, render_page)

    self.response.out.write(render_page)

class Cron(webapp.RequestHandler):
  def refresh(self, username, created_at):
    result_list = None
    summary = None
    try:
      result_list, summary = get_bitly_tweets(username)
    except:
      logging.error('Could not refresh %s', username)
      return
    add_to_recent_users(username)
    path = os.path.join(os.path.dirname(__file__), 'user.html')
    template_values = {
        'result_list' : result_list,
        'username' : username,
        'summary' : summary,
        'created_at' : created_at,
        'show_why': False,
    }
    render_page = template.render(path, template_values)
    memcache.add(username, render_page, 86400)
    add_user_report(username, render_page)
  
  def get(self):
    refresh = []
    time_now = datetime.datetime.now()
    result_list = db.GqlQuery("SELECT * FROM Report ORDER By last_updated LIMIT 20")
    for res in result_list:
      delta = time_now - res.last_updated
      logging.info('Trying to refresh %s (%d)', res.username, delta.seconds)
      if delta.seconds >= 86400:
        refresh.append(res.username)
        logging.info('Adding %s to the refresh list', res.username)
      if len(refresh) > 2:
        break

    if len(refresh) < 1:
      logging.error('Consider increasing the refresh LIMIT to greater than 20')
      
    for user in refresh:
      self.refresh(user, time_now)
      
def main():
  application = webapp.WSGIApplication([
      ('/', Home),
      (r'/u/(.*)', User),
      ('/about', About),
      ('/celebrities', Celebs),
      ('/flushmemcache', FlushMemcache),
      ('/flushdb', FlushDb),
      ('/cron', Cron),
      ], debug=True)
  wsgiref.handlers.CGIHandler().run(application)

if __name__ == '__main__':
  main()
