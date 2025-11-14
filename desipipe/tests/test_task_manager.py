import os
import time

from desipipe import Queue, Environment, TaskManager, FileManager


base_dir = './_tests/'


def test_app():

    from desipipe.task_manager import PythonApp

    def func(a, b):
        import numpy as np
        return a * b

    app = PythonApp(func)
    print(app.run(a=1, b=1))


def test_serialization():

    from desipipe.task_manager import serialize_function, deserialize_function

    def decorator(func): return func

    @decorator
    def func1(a: int, b: float, *args, c: int=4, **kwargs):
        if True:
            a = a**2
            if True:
                d = a
            return a * b * c
        else:
            return 1.
        if False:
            return 10.

    @decorator
    def func2(a: int, b: float, *args, c: int=4, **kwargs): return a**2 * b * c

    for func in [func1, func2]:
        name, code, vartypes, dlocals = serialize_function(func)
        print(code)
        func2 = deserialize_function(name, code, dlocals)
        assert func2(1, 2, c=5) == 10
        name, code, vartypes, dlocals = serialize_function(func2)
        print(name, code, vartypes, dlocals)
        func2 = deserialize_function(name, code, dlocals)
        assert func2(2, 2) == 32


def test_queue(spawn=True, run=False):

    tospawn = spawn
    if spawn: run = False

    from desipipe import spawn
    queue = Queue('test', base_dir=base_dir)
    queue.clear()
    provider = dict(provider='local')
    #if os.getenv('NERSC_HOST', None):
    #    provider = dict(time='00:01:00', nodes_per_worker=0.1)
    tm = TaskManager(queue, environ=dict(), scheduler=dict(max_workers=2), provider=provider)
    tm2 = tm.clone(scheduler=dict(max_workers=1), provider=dict(provider='local'))

    from test_def import common1

    def common2(size: int=10000, co=common1):
        return co(size=size)

    @tm.python_app
    def fraction(a, *args, co=common2, **kwargs):
        print('FRACTION')
        import logging
        from desipipe.utils import setup_logging
        setup_logging('info')
        logging.getLogger('FRACTION').info('logged')
        return co(**kwargs)

    @tm2.bash_app
    def echo(fractions):
        return ['echo', '-n', 'these are all results: {}'.format(fractions)]

    @tm2.python_app
    def average(fractions):
        import numpy as np
        return np.average(fractions) * 4.

    @tm2.python_app(name='average')
    def average2(fractions):
        return None

    @tm2.python_app(skip=True)
    def average3(fractions):
        return None

    t0 = time.time()
    fractions = [fraction(2, size=1000 + i) for i in range(5)]
    ech = echo(fractions)
    avg = average(fractions)
    avg2 = average2(fractions)
    assert average3(fractions) is None
    if tospawn:
        spawn(queue, timestep=2.3)
        print(queue.summary())
        print(ech.out())
        assert avg2.result() == avg.result()
        print(avg.result(), time.time() - t0)

    @tm2.bash_app(name=True, state='SUCCEEDED')
    def fraction():
        return None

    if tospawn:
        for frac in fractions:
            assert fraction().result() == frac.result()  # the previous fraction result is used

    @tm2.python_app
    def test_error(i):
        if i >= 2:
            raise ValueError(str(i))
        else:
            return i

    errors = [test_error(i) for i in range(4)]
    if tospawn:
        spawn(queue, timestep=2)
        errors = [error.err() for error in errors]
        assert not any(errors[:2])
        assert all(errors[2:])

    @tm2.python_app(name=True, state='SUCCEEDED')
    def test_error(i):
        return i + 10

    errors = [test_error(i) for i in range(4)]
    if tospawn:
        spawn(queue)
        errors = [error.result() for error in errors]
        assert errors[:2] == [0, 1]
        assert errors[2:] == [12, 13]

    if run:
        from desipipe.task_manager import TaskPickler, TaskUnpickler
        task = queue.pop()
        pkl = TaskPickler.dumps(task, reduce_app=False)
        task = TaskUnpickler.loads(pkl)
        print(task.kwargs)
        task.run()
        print(task.err, task.out)

    queue.pause()
    time.sleep(5)
    queue.resume()
    print('processes', queue.processes())
    print(queue.summary())

    for tid in queue.tasks(state='FAILED', property='tid'):
        del queue[tid]

    @tm2.python_app
    def error():
        print('ERROR')
        raise ValueError

    if tospawn:
        #spawn(queue, mode='stop_at_error')
        #print(queue.summary())
        for i in range(10):
            err = error()
        spawn(queue, mode='stop_at_error no_out')
        err.result()
        assert not err.out()
        print(queue.counts(state='FAILED'))

        for tid in queue.tasks(name='fraction', property='tid'):
            del queue[tid]

        queue.delete()


def test_cmdline():

    import subprocess
    queue = "'./_tests/*'"
    queue_single = "./_tests/test.sqlite"
    test_queue(spawn=False, run=False)
    subprocess.call(['desipipe', 'queues', '-q', queue])
    subprocess.call(['desipipe', 'tasks', '-q', queue_single, '--state', 'SUCCEEDED'])
    subprocess.call(['desipipe', 'time', '-q', queue_single])
    subprocess.call(['desipipe', 'delete', '-q', queue])
    subprocess.call(['desipipe', 'pause', '-q', queue])
    subprocess.call(['desipipe', 'resume', '-q', queue])
    subprocess.call(['desipipe', 'spawn', '-q', queue])
    subprocess.call(['desipipe', 'retry', '-q', queue, '--state', 'SUCCEEDED', '--spawn'])


def test_file(spawn=True):

    tospawn = spawn

    txt = 'hello world!'

    fm = FileManager()
    fm.append(dict(description='added file', id='input', filetype='text', path=os.path.join(base_dir, 'hello_in_{i:d}.txt'), options={'i': range(10)}))
    for fi in fm:
        fi.save(txt)
    fm.append(fm[0].clone(id='output', path=os.path.join(base_dir, 'hello_out_{i:d}.txt')))

    queue = Queue('test2', base_dir=base_dir)
    provider = dict(provider='local')
    #if os.getenv('NERSC_HOST', None):
    #    provider = dict(time='00:02:00', nodes_per_worker=0.1)
    tm = TaskManager(queue, environ=dict(), scheduler=dict(max_workers=2), provider=provider)

    @tm.python_app
    def copy(text_in, text_out):
        text = text_in.load()
        text += ' this is my first message'
        print('saving', text_out.filepath)
        text_out.save(text)

    results = []
    for fi in fm:
        results.append(copy(fi.get(id='input'), fi.get(id='output')))

    if tospawn:
        from desipipe import spawn
        spawn(queue)
        for res in results:
            print('out', res.out())
            print('err', res.err())

    queue.delete()


def test_mpi():

    from desipipe import setup_logging
    setup_logging()

    queue = Queue('test3', base_dir=base_dir)
    provider = dict(provider='local')
    tm = TaskManager(queue, environ=dict(), scheduler=dict(max_workers=2), provider=provider)


def test_slurm():
    from desipipe.provider import decode_slurm_time
    assert decode_slurm_time('51') == (0, 0, 51, 0)
    assert decode_slurm_time('51:20') == (0, 0, 51, 20)
    assert decode_slurm_time('02:51:20') == (0, 2, 51, 20)
    assert decode_slurm_time('3-02') == (3, 2, 0, 0)
    assert decode_slurm_time('3-02:30') == (3, 2, 30, 0)
    assert decode_slurm_time('3-02:30:08') == (3, 2, 30, 8)


if __name__ == '__main__':

    test_serialization()
    #test_app()
    test_queue(spawn=True, run=False)
    #test_cmdline()
    #test_file(spawn=True)
    #test_mpi()
    test_slurm()