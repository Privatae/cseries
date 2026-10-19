"""The standard English stop list — the Glasgow Information Retrieval
Group's, exactly as scikit-learn ships it (sklearn/feature_extraction/
_stop_words.py, v1.5.2, BSD-3-Clause, Copyright (c) 2007-2024 The
scikit-learn developers; original at
http://ir.dcs.gla.ac.uk/resources/linguistic_utils/stop_words).

Vendored, not imported: scikit-learn is not a dependency of this image, and
one word list does not justify it. Used by runtime/turn_types.py so a
correction teaches a type only words that mean something — the standard
tool for a keyword classifier, in place of a list grown by hand.
"""
from __future__ import annotations

ENGLISH_STOP_WORDS: frozenset[str] = frozenset({
    "a", "about", "above", "across", "after", "afterwards", "again", "against", "all",
    "almost", "alone", "along", "already", "also", "although", "always", "am", "among",
    "amongst", "amoungst", "amount", "an", "and", "another", "any", "anyhow", "anyone",
    "anything", "anyway", "anywhere", "are", "around", "as", "at", "back", "be",
    "became", "because", "become", "becomes", "becoming", "been", "before",
    "beforehand", "behind", "being", "below", "beside", "besides", "between", "beyond",
    "bill", "both", "bottom", "but", "by", "call", "can", "cannot", "cant", "co",
    "con", "could", "couldnt", "cry", "de", "describe", "detail", "do", "done", "down",
    "due", "during", "each", "eg", "eight", "either", "eleven", "else", "elsewhere",
    "empty", "enough", "etc", "even", "ever", "every", "everyone", "everything",
    "everywhere", "except", "few", "fifteen", "fifty", "fill", "find", "fire", "first",
    "five", "for", "former", "formerly", "forty", "found", "four", "from", "front",
    "full", "further", "get", "give", "go", "had", "has", "hasnt", "have", "he",
    "hence", "her", "here", "hereafter", "hereby", "herein", "hereupon", "hers",
    "herself", "him", "himself", "his", "how", "however", "hundred", "i", "ie", "if",
    "in", "inc", "indeed", "interest", "into", "is", "it", "its", "itself", "keep",
    "last", "latter", "latterly", "least", "less", "ltd", "made", "many", "may", "me",
    "meanwhile", "might", "mill", "mine", "more", "moreover", "most", "mostly", "move",
    "much", "must", "my", "myself", "name", "namely", "neither", "never",
    "nevertheless", "next", "nine", "no", "nobody", "none", "noone", "nor", "not",
    "nothing", "now", "nowhere", "of", "off", "often", "on", "once", "one", "only",
    "onto", "or", "other", "others", "otherwise", "our", "ours", "ourselves", "out",
    "over", "own", "part", "per", "perhaps", "please", "put", "rather", "re", "same",
    "see", "seem", "seemed", "seeming", "seems", "serious", "several", "she", "should",
    "show", "side", "since", "sincere", "six", "sixty", "so", "some", "somehow",
    "someone", "something", "sometime", "sometimes", "somewhere", "still", "such",
    "system", "take", "ten", "than", "that", "the", "their", "them", "themselves",
    "then", "thence", "there", "thereafter", "thereby", "therefore", "therein",
    "thereupon", "these", "they", "thick", "thin", "third", "this", "those", "though",
    "three", "through", "throughout", "thru", "thus", "to", "together", "too", "top",
    "toward", "towards", "twelve", "twenty", "two", "un", "under", "until", "up",
    "upon", "us", "very", "via", "was", "we", "well", "were", "what", "whatever",
    "when", "whence", "whenever", "where", "whereafter", "whereas", "whereby",
    "wherein", "whereupon", "wherever", "whether", "which", "while", "whither", "who",
    "whoever", "whole", "whom", "whose", "why", "will", "with", "within", "without",
    "would", "yet", "you", "your", "yours", "yourself", "yourselves",
})

assert len(ENGLISH_STOP_WORDS) == 318
