import popflash_api as pf
import mlcrate as mlc
import dateparser
from collections import defaultdict
import numpy as np
from itertools import groupby
import os
import discord
import asyncio
from threading import Thread
import copy

from trueskill import Rating, TrueSkill

from flask import Flask
from flask_restful import Resource, Api, reqparse
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
api = Api(app)

if not os.path.exists('matches/'):
  os.mkdir('matches/')

class Player:
  def __init__(self, name, id):
    self.name = name
    self.id = id
    self.games = 0

  def __repr__(self):
    return self.name

  def __eq__(self, other):
    return self.id.__eq__(other.id)

  def __hash__(self):
    return self.id.__hash__()

from scipy.stats import logistic

class TrueSkillTracker:
  def __init__(self, mu=1000, sigma=8.33*40/2, beta=4.16*40, tau=0.083*40, mode='match', min_ranked_matches=6):
    self.min_ranked_matches = min_ranked_matches
    assert mode in ['round', 'match']
    self.mode = mode
    self.ts = TrueSkill(mu=mu, sigma=sigma, beta=beta, tau=tau, draw_probability=0) #, backend=(logistic.cdf, logistic.pdf, logistic.ppf))
    
    self.skills = defaultdict(lambda: self.ts.create_rating())
    self.hltv = defaultdict(int)
    self.player_counts = defaultdict(int)
    self.skill_history = [self.skills.copy()]
    self.match_ids = [] # To avoid repeating matches
    self.hltv = 0.75

    self.player_rounds_played = defaultdict(int)
    self.player_rounds_won = defaultdict(int)
    self.player_hltv_history = defaultdict(list)
    #print(f'RATING mu={mu} sigma={sigma} beta={beta}, tau={tau}, hltv={hltv}, mode=GAME')

  def process_match(self, match):
    if match['match_id'] in self.match_ids:
      print('Warning: tried to process the same match twice')
      return

    self.match_ids.append(match['match_id'])
    
    trace = match['match_id'] == '1149271'
    if trace:
      print('TRACING MATCH', match['match_id'])
    
    t1table = match['team1table']
    t2table = match['team2table']
    t1players = [Player(p['Name'], p['id']) for _, p in t1table.iterrows()]
    t2players = [Player(p['Name'], p['id']) for _, p in t2table.iterrows()]

    if trace:
      print('* before match:')
      trace_skill1 = {p: int(self.skills[p].mu) for p in t1players}
      trace_skill2 = {p: int(self.skills[p].mu) for p in t2players}
      print('team1:', trace_skill1)
      print('team2:', trace_skill2)

    for p in t1players:
      self.player_counts[p] += 1
      self.player_rounds_played[p] += match['team1score'] + match['team2score']
      self.player_rounds_won[p] += match['team1score']
    for p in t2players:
      self.player_counts[p] += 1
      self.player_rounds_played[p] += match['team1score'] + match['team2score']
      self.player_rounds_won[p] += match['team2score']

    # Calculate number of wins each team got
    if self.mode == 'match':
      round_diff = match['team1score'] - match['team2score']
      rounds = [1] if round_diff >= 0 else [2]
      if match['team1score'] == match['team2score']:
        rounds = [0] # special case!!

    elif self.mode == 'round':
      rounds = [1]*match['team1score'] + [2]*match['team2score']

    elif self.mode == 'round_diff':
      round_diff = match['team1score'] - match['team2score']
      rounds = [1]*round_diff if round_diff > 0 else [2]*(-round_diff)
      if round_diff == 0: # draw
        rounds = [0]

    np.random.seed(42)
    rounds = np.random.permutation(rounds)

    if trace:
      print('Generated round sequence:', rounds)
    
    for i, r in enumerate(rounds):
      if trace:
        print('* round', i, 'winner team', r)

      t1skills = [self.skills[p] for p in t1players]
      t2skills = [self.skills[p] for p in t2players]

      t1weights = np.array([p['HLTV'] for _, p in t1table.iterrows()])
      t2weights = np.array([p['HLTV'] for _, p in t2table.iterrows()])

      # Keep track of HLTVs
      for (p, hltv) in zip(t1players + t2players, t1weights.tolist() + t2weights.tolist()):
        self.player_hltv_history[p].append(hltv)
      
      #### Calculating ratings (weighted by HLTV)

      if r == 0: # draw
        ranks = [0, 0]
        t1weights = [1, 1, 1, 1, 1] # Not sure how to do ratings on a draw
        t2weights = [1, 1, 1, 1, 1]

      else:
        ranks = [1, 0] if r==2 else [0, 1] 

        if r==1:
          t2weights = 1/t2weights
        else:
          t1weights = 1/t1weights

        t1weights **= self.hltv
        t2weights **= self.hltv

        t1weights /= (t1weights.sum() / 5)
        t2weights /= (t2weights.sum() / 5)

      if trace:
        print('weights:', np.around(t1weights, 1), np.around(t2weights, 1))

      newt1skills, newt2skills = self.ts.rate([t1skills, t2skills], ranks, weights=[t1weights, t2weights])

      if trace:
        print('team1:', {p: round(newt1skills[i].mu - self.skills[p].mu, 2) for i, p in enumerate(t1players)})
        print('team2:', {p: round(newt2skills[i].mu - self.skills[p].mu, 2) for i, p in enumerate(t2players)})

      for p, n in zip(t1players, newt1skills):
        self.skills[p] = n
      for p, n in zip(t2players, newt2skills):
        self.skills[p] = n

    self.skill_history.append(self.skills.copy())

    if trace:
      print('* OVERALL CHANGE:')
      print('team1:', {p: round(self.skills[p].mu - self.skill_history[-2][p].mu, 2) for i, p in enumerate(t1players)})
      print('team2:', {p: round(self.skills[p].mu - self.skill_history[-2][p].mu, 2) for i, p in enumerate(t2players)})



# Load seed matches from before we had user submissions. From collect_seed_matches.py
matches = mlc.load('seedmatches4.pkl')
print(len(matches), 'seed matches')

# Add matches submitted by users
for match_id in [x for x in open('submitted_matches.txt').read().split('\n') if x]:
  try:
    match = mlc.load('matches/{}.pkl'.format(match_id))
  except:
    match = pf.get_match(match_id)
    mlc.save(match, 'matches/{}.pkl'.format(match_id))

  matches.append(match)

# Bring forward old dates from before the format was updated
for m in matches:
  if isinstance(m['date'], str):
    print('updating date', m['match_id'])
    m['date'] = dateparser.parse(m['date'])
    mlc.save(m, 'matches/{}.pkl'.format(m['match_id']))

matches = sorted(matches, key=lambda x: x['date'])

ts = TrueSkillTracker()

print('Loaded', len(matches), 'Matches')

for match in matches:
  ts.process_match(match)

################ WEB API

class Matches(Resource):
  def get(self):
    ret_matches = copy.deepcopy(matches)

    for match in ret_matches:
      match['team1table'].index = match['team1table']['player_link'].apply(lambda x: x.split('/')[-1])
      match['team2table'].index = match['team2table']['player_link'].apply(lambda x: x.split('/')[-1])
      match['team1table'] = match['team1table'].to_dict(orient='index')
      match['team2table'] = match['team2table'].to_dict(orient='index')
      match['date'] = match['date'].isoformat()

    return ret_matches


class PlayerRankings(Resource):
  def get(self):
    ret = []

    for user, skill in ts.skills.items():
      if ts.player_counts[user] < ts.min_ranked_matches: 
        continue

      user_skill_history = [{'SR': h[user].mu, 'date': '' if i==0 else matches[i-1]['date'].isoformat(), 'match_id': 0 if i==0 else matches[i-1]['match_id']} for i,h in enumerate(ts.skill_history)]
      user_skill_history = [list(g)[0] for k,g in groupby(user_skill_history, lambda x: x['SR'])]

      user_last_diff = user_skill_history[-1]['SR'] - user_skill_history[-2]['SR']
      
      user_rwp = (ts.player_rounds_won[user] / ts.player_rounds_played[user])
      user_hltv = np.mean(ts.player_hltv_history[user])

      ret.append({'username': user.name, 'SR': int(skill.mu), 'SRvar': int(skill.sigma), 'matches_played': ts.player_counts[user], 'user_id': user.id, 
                  'last_diff': int(user_last_diff), 'user_skill_history': user_skill_history, 'rwp': user_rwp, 'hltv': user_hltv})
    return ret

parser = reqparse.RequestParser()
parser.add_argument('match_url')

class SubmitMatch(Resource):
  def post(self):
    args = parser.parse_args()
    match_id = args['match_url'].split('/')[-1]
    if not match_id.isnumeric():
      return "Bad popflash match url provided", 400

    if match_id in ts.match_ids:
      return "Match already processed", 400

    match_url = 'https://popflash.site/match/' + match_id

    match = pf.get_match(match_url)
    matches.append(match)
    
    open('submitted_matches.txt', 'a').write(match_id + '\n')
    mlc.save(match, 'matches/{}.pkl'.format(match_id))

    skills_before = ts.skills.copy()

    # Will do nothing if match has already been processed
    ts.process_match(match)

    # Response stuff for discord
    resp = {}
    t1,t2 = 'WL' if match['team1score']>match['team2score'] else 'LW' if match['team1score']<match['team2score'] else 'TT'
    resp = {
        'team1status': "{} - {}".format(t1, match['team1score']),
        'team2status': "{} - {}".format(t2, match['team2score'])
    }

    t1stats = []
    for _, row in match['team1table'].iterrows():
      player = Player(row['Name'], row['id'])
      oldskill = skills_before[player].mu
      newskill = ts.skills[player].mu
      diff = newskill - oldskill
      t1stats.append('{} - {} **({}{})**'.format(player.name, int(newskill), '+' if diff>0 else '', int(diff)))

    t2stats = []
    for _, row in match['team2table'].iterrows():
      player = Player(row['Name'], row['id'])
      oldskill = skills_before[player].mu
      newskill = ts.skills[player].mu
      diff = newskill - oldskill
      t2stats.append('{} - {} **({}{})**'.format(player.name, int(newskill), '+' if diff>0 else '', int(diff)))

    resp['team1stats'] = '\n'.join(t1stats)
    resp['team2stats'] = '\n'.join(t2stats)

    resp['time'] = match['date'].isoformat()
    resp['image'] = match['map_image']

    return resp, 200

api.add_resource(PlayerRankings, '/rankings')
api.add_resource(SubmitMatch, '/submit_match')
api.add_resource(Matches, '/matches')


# ronan = ([h[Player('Porkypus', '758084')].mu for h in ts.skill_history])
# ronan_var = np.array([h[Player('Porkypus', '758084')].sigma for h in ts.skill_history])
# ronan = np.array([k for k, g in groupby(ronan)])
# ronan_var = np.array([k for k, g in groupby(ronan_var)])
# import matplotlib.pyplot as plt
# plt.plot(ronan)
# plt.fill_between(np.arange(len(ronan)), ronan-ronan_var, ronan+ronan_var, alpha=0.2)
# plt.ylim(800, 2000)
# plt.show()
# print(ronan)

if __name__ == '__main__':
    app.run(debug=True, port=7355)
    
