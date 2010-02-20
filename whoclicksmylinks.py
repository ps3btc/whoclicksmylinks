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
import wsgiref.handlers
from django.utils import simplejson as json
from google.appengine.api import memcache
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template

DATETIME_STRING_FORMAT = '%a %b %d %H:%M:%S +0000 %Y'
BITLY_KEY = 'R_5e2a59607054db4cf2dc101cd84bf4fd'
BITLY_LOGIN = 'hnag'
ALL_USERS_LIST = '6376097130910121270'

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
    if word.find('http://bit.ly') != -1:
      return word.split('/')[-1:][0]
  return None

def get_clicks(shortcut):
  url = ('http://api.bit.ly/stats?version=2.0.1&hash=%s&login=%s&apiKey=%s'
         % (shortcut, BITLY_LOGIN, BITLY_KEY))
  handle = urllib2.urlopen(url)
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
  handle=urllib2.urlopen(url)

  results = []
  reference_epoch = time.time()
  data = json.loads(handle.read())
  followers_count = float(data[0]['user']['followers_count'])
  total_clicks = 0
  total_links = 0
  for result in data:
    text = result['text']
    if text.find('bit.ly') != -1 and text.find('RT') == -1:
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
        'recent_users': get_recent_users(),
        }))

class About(webapp.RequestHandler):
  def get(self):
    path = os.path.join(os.path.dirname(__file__), 'about.html')
    self.response.out.write(template.render(path, {
        'show_why': False
        }))

class FlushMemcache(webapp.RequestHandler):
  def get(self):
    if memcache.flush_all():
      logging.info('memcache: flushed all')
    self.redirect('/', permanent=False)

class User(webapp.RequestHandler):
  def get(self, username):
    username = unicode(urllib.unquote(username), 'utf-8')
    render_page = memcache.get(username)
    if render_page:
      self.response.out.write(render_page)
      logging.info('memcache: retrieve results for %s', username)
      return
    
    result_list = None
    summary = None
    try:
      result_list, summary = get_bitly_tweets(username)
    except:
      self.redirect('/', permanent=False)
      return

    add_to_recent_users(username)
    path = os.path.join(os.path.dirname(__file__), 'user.html')
    template_values = {
        'result_list' : result_list,
        'summary' : summary,
        'show_why': False,
    }
    render_page = template.render(path, template_values)
    memcache.add(username, render_page, 86400)
    self.response.out.write(render_page)

def main():
  application = webapp.WSGIApplication([
      ('/', Home),
      (r'/u/(.*)', User),
      ('/about', About),
      ('/flushmemcache', FlushMemcache),
      ], debug=True)
  wsgiref.handlers.CGIHandler().run(application)

if __name__ == '__main__':
  main()
