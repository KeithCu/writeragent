import uno
from plugin.testing_runner import native_test

@native_test
def test_break_iterator_sentences(uno_context=None):
    if uno_context is None:
        return
    smgr = uno_context.ServiceManager
    break_iterator = smgr.createInstanceWithContext("com.sun.star.i18n.BreakIterator", uno_context)
    
    locale = uno.createUnoStruct("com.sun.star.lang.Locale")
    locale.Language = "en"
    locale.Country = "US"
    
    text = "Hello world. This is a test. Wait... what? Yes! It costs $5.00."
    
    pos = 0
    sentences = []
    while pos < len(text):
        # endOfSentence(Text, nStartPos, nLocale)
        end_pos = break_iterator.endOfSentence(text, pos, locale)
        
        # If we didn't advance, break
        if end_pos <= pos:
            # Maybe there are spaces at the end? Let's just break for this experiment.
            # wait, actually endOfSentence might just return the index.
            break
            
        sentence = text[pos:end_pos]
        sentences.append((pos, sentence))
        pos = end_pos
        
    print("Sentences:", sentences)
    # Write output to a file so we can see it
    with open('/tmp/break_iterator_out.txt', 'w') as f:
        f.write(str(sentences))

