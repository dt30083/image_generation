"""Streaming multi-pattern counting, linear in text plus reported matches."""
from collections import Counter, deque

class Matcher:
    def __init__(self, patterns):
        self.next=[{}]; self.fail=[0]; self.out=[[]]; self.state=0; self.counts=Counter()
        for pattern in patterns:
            state=0
            for character in pattern:
                if character not in self.next[state]:
                    self.next[state][character]=len(self.next)
                    self.next.append({}); self.fail.append(0); self.out.append([])
                state=self.next[state][character]
            self.out[state].append(pattern)
        queue=deque(self.next[0].values())
        while queue:
            state=queue.popleft()
            for character,child in self.next[state].items():
                queue.append(child); fallback=self.fail[state]
                while fallback and character not in self.next[fallback]: fallback=self.fail[fallback]
                self.fail[child]=self.next[fallback].get(character,0)
                self.out[child].extend(self.out[self.fail[child]])

    def feed(self, text):
        state=self.state
        for character in text:
            while state and character not in self.next[state]: state=self.fail[state]
            state=self.next[state].get(character,0)
            for pattern in self.out[state]: self.counts[pattern]+=1
        self.state=state
